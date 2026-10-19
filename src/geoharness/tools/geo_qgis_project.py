"""Create, inspect and export editable projects in the separate QGIS runtime."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from pydantic_core import PydanticCustomError

from geoharness.cartography.derivation import MapDerivation, derive_frame
from geoharness.cartography.mutation_control import guard_map_mutations
from geoharness.cartography.retry_guard import guard_map_errors
from geoharness.cartography.snapshot import FrozenQgisInputs, freeze_qgis_inputs
from geoharness.cartography.workflow import map_identity, refund_map_render, reserve_map_render
from geoharness.qgis.colors import normalize_color
from geoharness.tools.geo_qgis import qgis_read_roots


class QgisCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | int | float | bool | None = Field(description=(
        "必填。分类字段中的真实取值；保留源类型（字符串、整数、浮点、布尔或 null）。"
        "必须与数据中实际值逐项对应，不能用图例文案或行号代替。null 也是合法类别键。"
    ))
    label: str = Field(default="", description=(
        "可选。图例显示文字；留空使用 value 本身。"
        "只有在核实实际含义和单位后才翻译/改写，不要改变数值含义。"
    ))
    color: str = Field(description=(
        "必填。该类别填充色，#RRGGBB（如 #d73027）；rgba 会规范化为 Qt ARGB。"
    ))
    _color = field_validator("color")(normalize_color)
    size_mm: float | None = Field(default=None, gt=0, le=50, description=(
        "可选。该类别单独的符号尺寸（毫米）；省略继承 QgisVectorStyle.size_mm。"
        "改变尺寸会改变纸面占位与重叠风险。"
    ))
    width_mm: float | None = Field(default=None, ge=0, le=20, description=(
        "可选。该类别单独的描边宽度（毫米）；省略继承样式默认值。"
    ))


class QgisRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lower: float = Field(allow_inf_nan=False, description=(
        "必填。实际数值字段单位下的下端（含）；必须有限且 <= upper。"
        "不要在这些边界里偷偷换算单位；先确认字段本身单位。"
    ))
    upper: float = Field(allow_inf_nan=False, description=(
        "必填。实际数值字段单位下的上端（含）；必须有限且 >= lower。"
        "区间必须有序且互不重叠，标签说明单位/边界含义。"
    ))
    label: str = Field(default="", description=(
        "可选。图例区间的显示文字；说明单位和边界是否包含，"
        "不要声称数据不具备的精度。"
    ))
    color: str = Field(description="必填。该区间填充色，#RRGGBB（如 #fee08b）。")
    _color = field_validator("color")(normalize_color)
    size_mm: float | None = Field(default=None, gt=0, le=50, description=(
        "可选。该区间单独的符号尺寸（毫米）；省略继承样式默认值。"
    ))
    width_mm: float | None = Field(default=None, ge=0, le=20, description=(
        "可选。该区间单独的描边宽度（毫米）；省略继承样式默认值。"
    ))

    @model_validator(mode="after")
    def validate_bounds(self) -> QgisRange:
        if self.lower > self.upper:
            raise ValueError("graduated range lower must not exceed upper")
        return self


class QgisVectorStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    renderer: Literal["single", "categorized", "graduated"] = Field(default="single", description=(
        "必填，默认 single。single = 单一颜色；categorized = 每个精确值一个类别"
        "（必须提供 field + categories）；graduated = 有序数值区间"
        "（必须提供 field + ranges）。按实测字段语义选择，不能使用 Processing 参数"
        "或 geo_cartography 的桥接样式。"
    ))
    color: str = Field(default="#4c78a8", description=(
        "可选。renderer=single 时的填充色 #RRGGBB；categorized/graduated 时忽略，"
        "颜色来自各自 category/range。"
    ))
    field: str = Field(default="", description=(
        "可选。categorized/graduated 使用的真实属性字段名。renderer=single 时留空。"
        "必须与 source schema 完全一致；不能使用 geometry 列、别名或图例标题。"
    ))
    categories: list[QgisCategory] = Field(default_factory=list, description=(
        "renderer=categorized 时必填。每项含真实源 value 与 color，value 必须唯一。"
        "只列准备画出并可解释的类别；图例文字必须与实际含义一致。"
    ))
    ranges: list[QgisRange] = Field(default_factory=list, description=(
        "renderer=graduated 时必填。按数值下界自动排序，区间不得重叠；"
        "标签写清单位和是否包含边界。"
    ))
    opacity: float = Field(default=1.0, ge=0, le=1, description=(
        "可选，默认 1。整个图层不透明度，0 完全不可见。"
        "若需保留描边和标注、只让填充变透明，请用 fill_opacity。"
    ))
    fill_opacity: float = Field(default=1.0, ge=0, le=1, description=(
        "可选，默认 1。仅符号填充透明度；描边和标注仍可见。"
        "空心圆可透出底层分类点，但不能借此隐藏必须呈现的面。"
    ))
    size_mm: float = Field(default=3, gt=0, le=50, description=(
        "可选，默认 3。纸面上的点直径或线/符号尺寸（毫米），不是数据距离。"
        "最终比例尺下仍可能重叠；需结合图框和实体分布检查。"
    ))
    width_mm: float = Field(default=0.3, ge=0, le=20, description=(
        "可选，默认 0.3。描边宽度（毫米）；0 表示无描边。"
    ))
    outline_color: str = Field(default="#ffffff", description=(
        "可选。描边颜色 #RRGGBB。选择与填充对比、而不是与页面背景对比的颜色。"
    ))
    _colors = field_validator("color", "outline_color")(normalize_color)
    obstacle: bool | None = Field(default=None, description=(
        "可选。PAL 标注障碍：None 保持默认；true 保护此几何不被其他图层标注压盖，"
        "包括未标注的线/面。"
    ))
    obstacle_factor: float = Field(default=1, ge=0, le=10, description=(
        "可选，默认 1。obstacle=true 时按符号实际范围的放大倍数；只在确有间距需求时增大。"
    ))

    @model_validator(mode="after")
    def validate_categories(self) -> QgisVectorStyle:
        if self.renderer == "categorized" and (not self.field or not self.categories):
            raise ValueError("categorized styling requires field and explicit categories/colors")
        if self.renderer == "graduated" and (not self.field or not self.ranges):
            raise ValueError("graduated styling requires field and explicit ranges/colors")
        if self.renderer != "categorized" and self.categories:
            raise ValueError("categories require renderer=categorized")
        if self.renderer != "graduated" and self.ranges:
            raise ValueError("ranges require renderer=graduated")
        if self.renderer == "single" and self.field:
            raise ValueError("field requires renderer=categorized or graduated")
        values = [json.dumps(category.value, sort_keys=True) for category in self.categories]
        if len(values) != len(set(values)):
            raise ValueError("category values must be unique")
        # A reversed legend order is a harmless permutation, not an invalid interval.
        # Keep each interval's label and color together; actual overlap still fails.
        object.__setattr__(self, "ranges", sorted(self.ranges,
                           key=lambda item: (item.lower, item.upper)))
        for previous, current in zip(self.ranges, self.ranges[1:]):
            if current.lower < previous.upper or current.upper <= previous.upper:
                raise ValueError("graduated ranges must be ordered and non-overlapping")
        return self



class QgisLabelStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font_family: str = Field(default="Microsoft YaHei", description=(
        "可选。已安装字体族；必须与布局测量使用同一字体，换字体会改变真实文字宽度。"
        "系统已安装字体优先填此项。"
    ))
    font_file: str = Field(default="", description=(
        "可选。workspace/data 读取根内的 TTF/TTC/OTF 字体文件。"
        "已安装字体优先用 font_family；系统目录的显式路径不在授权读取范围。"
    ))
    size_pt: float = Field(default=9, ge=5, le=40, description=(
        "可选，默认 9。标注字号（pt），不是毫米；纸面、符号和标注必须用各自单位。"
    ))
    color: str = Field(default="#222222", description=(
        "可选。标注文字颜色 #RRGGBB；选择与实际填充形成对比的颜色。"
    ))
    buffer_size_mm: float = Field(default=0.8, ge=0, le=5, description=(
        "可选，默认 0.8。文字白色晕圈半径（毫米）；0 关闭。"
    ))
    buffer_color: str = Field(default="#ffffff", description=(
        "可选。晕圈颜色 #RRGGBB；通常为白色或能隔开文字与底图的颜色。"
    ))
    placement: Literal["auto", "around_point", "over_point", "parallel", "horizontal"] = Field(
        default="auto", description=(
            "可选，默认 auto。auto 交由 PAL；around_point/over_point 仅用于 Point/MultiPoint；"
            "parallel 用于 LineString/MultiLineString；horizontal 可用于面。"
            "选择前先核对实际几何类型。"
        ))
    _colors = field_validator("color", "buffer_color")(normalize_color)
    distance_mm: float = Field(default=1.5, ge=0, le=20, description=(
        "可选，默认 1.5。around_point 放置的纸面距离（毫米）；over_point 忽略该值。"
    ))
    offset_x_mm: float = Field(default=0, ge=-20, le=20, description=(
        "可选。over_point 的固定水平偏移（毫米）；around_point 忽略 X/Y 偏移。"
        "around_point 用 distance_mm，确需固定偏移时显式改 over_point。"
    ))
    offset_y_mm: float = Field(default=0, ge=-20, le=20, description=(
        "可选。over_point 的固定垂直偏移（毫米）；around_point 用 distance_mm 代替。"
    ))
    priority: int = Field(default=5, ge=0, le=10, description=(
        "可选，默认 5。PAL 标注优先级，0 最低，10 最高；用于决定冲突时谁先放置。"
    ))



class QgisRasterStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    renderer: Literal["default", "hillshade"] = Field(default="default", description=(
        '默认default按标准拉伸显示波段；hillshade按高程波段计算阴影。'
    ))
    band: int = Field(default=1, ge=1, description=(
        '默认1，从1开始的波段号；先检查波段真实含义，不能默认第一波段是高程。'
    ))
    azimuth: float = Field(default=315, ge=0, le=360, description=(
        '默认315，光源方位角，北向起顺时针角度0..360，315为西北。'
    ))
    altitude: float = Field(default=45, gt=0, le=90, description=(
        '默认45，光源高度角，单位度，须大于0且不超过90。'
    ))


class QgisLeaderBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source_layer: Annotated[int, Field(strict=True, ge=0)] | str = Field(description=(
        '真实要素锚点的层索引（从0开始）或准确layer_key，不用编号层代替。'
    ))
    labels_layer: Annotated[int, Field(strict=True, ge=0)] | str = Field(description=(
        '移动后标签锚点的层索引（从0开始）或layer_key。'
    ))
    key_field: str = Field(min_length=1, description=(
        '两层均存在且各自唯一的业务键，按实体连接引线，不按行序。'
    ))
    metric_crs: str = Field(min_length=1, description=(
        '必填，单位米的投影CRS，用于量测引线间隙，不能用经纬度。'
    ))
    tolerance_m: float = Field(default=.01, ge=0, description=(
        '默认0.01，端点校验容差，单位米，非负。'
    ))
    source_gap_m: float = Field(default=0, ge=0, description=(
        '默认0，源点与引线起点间隙，单位米，非负。'
    ))
    target_gap_m: float = Field(default=0, ge=0, description=(
        '默认0，引线终点与标签锚点间隙，单位米，非负。'
    ))


class QgisProjectLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["map", "table_only"] = Field(default="map", description=(
        "图层角色。map = 正常绘制到地图；table_only = 仅登记/校验矢量记录供名单表使用，"
        "永不参与地图绘制，也不能出现在任何图框的 render/focus/label 列表。"
    ))
    _normalized_style_fields: tuple[str, ...] = PrivateAttr(default=())

    file_path: str = Field(min_length=1, description=(
        "必填。数据检查返回的实际本地矢量/栅格路径，或前置工具产物的准确路径。"
        "不能传目录、URL、模糊的 catalog 名称或猜测文件名；"
        "同一工程尽量复用同一来源以保证 layer_key 稳定。"
    ))
    kind: Literal["vector", "raster"] = Field(default="vector", description=(
        "必填，默认 vector。vector = GeoPackage/Shapefile/GeoJSON；raster = GeoTIFF/DEM/影像。"
        "它决定可用的样式字段：vector 用 style/label_*，raster 用 raster_style；"
        "不要把矢量样式字段写到栅格图层。"
    ))
    show_in_legend: bool | None = Field(default=None, description=(
        "可选。是否在图例中显示。空值时矢量/单波段栅格显示，RGB 影像默认隐藏；"
        "辅助编号、引线、底图可显式设 false，但仍会绘制。"
    ))
    name: str = Field(default="", description=(
        "可选。图例/诊断中的用户可见名称；不负责选择数据源。"
        "GeoPackage 内部图层选择请用 layer_name，二者不要混淆。"
    ))
    layer_key: str = Field(default="", max_length=128, description=(
        "可选但强烈推荐。跨版本稳定的业务图层身份；同一图层替换文件时保留原 key。"
        "后续 edits 的 layer_key、图框的 focus/render/label 引用和 leader_binding 都应使用它。"
        "不要用显示名称、图例文字或 0 基索引代替。"
    ))
    layer_name: str = Field(default="", description=(
        "可选。多图层 GeoPackage 内部的准确源图层名；单图层文件或来源唯一时留空。"
        "它不是图例显示名称（name）。"
    ))
    style: QgisVectorStyle | None = Field(default=None, description=(
        "矢量符号设置。renderer、field、categories/ranges、size_mm、颜色等必须放在此对象内；"
        "按实际字段语义选择 single/categorized/graduated，不能照搬 geo_cartography 的桥接字段。"
    ))
    raster_style: QgisRasterStyle | None = Field(default=None, description=(
        "栅格显示设置，仅 kind='raster' 时使用：default 或 hillshade、band、azimuth、altitude。"
        "band 是 1 基索引且必须先核实波段含义。"
    ))
    label_field: str = Field(default="", description=(
        "可选。实际存在的属性字段或已验证的 number_field，其值会作为可见标注。"
        "留空表示只画符号不标注。业务 ID 与显示编号不同；有序编号请用派生 number_field。"
    ))
    leader_binding: QgisLeaderBinding | None = Field(default=None, description=(
        "可选。直线引线契约：把真实源要素与移动后的标注锚点按业务键绑定。"
        "source_layer 必须是真实点位、labels_layer 必须是锚点层，key_field 在两层唯一；"
        "preflight 会检查当前端点，视觉审查不能替代几何证据。"
    ))
    label_style: QgisLabelStyle | None = Field(default=None, description=(
        "可选。label_field 的字体、字号、缓冲、放置方式和偏移。"
        "字号使用 pt；placement 必须匹配实际几何类型。"
    ))
    required: bool = Field(default=True, description=(
        "可选，默认 true。true 表示每个实际渲染图框都要看到该图层要素；"
        "仅当背景/上下文图层缺失不会删除已声明主体时才设 false。"
        "系统会拒绝从未进入任何图框的 required 图层。"
    ))
    expected_feature_count: int | None = Field(default=None, ge=0, description=(
        "可选。加载后必须精确匹配的源要素数。只有独立已知该数量时才填；"
        "不要为了通过校验猜一个数。"
    ))
    minimum_visible_features: int = Field(default=1, ge=0, description=(
        "可选，默认 1。图框内至少可见的要素数；上下文图层可设 0。"
        "需要对具名主体要求完整覆盖时，应在图框 requirements 中明确写出，而不是只调大这个数。"
    ))
    derivation: MapDerivation | None = Field(
        default=None,
        description=(
            "可选。对 file_path 的已验证地图子集/字段选择/新增注记（feature_indices、keep_fields、"
            "annotations）。几何保留，新增注记不能覆盖源字段；不要用它伪造不存在的源属性。"
        ),
    )



    @model_validator(mode="wrap")
    @classmethod
    def normalize_style_fields(cls, value, handler):
        moved = ()
        if isinstance(value, dict):
            moved = tuple(key for key in ("renderer", "field", "categories") if key in value)
            if moved:
                nested = value.get("style")
                if isinstance(nested, QgisVectorStyle):
                    nested = nested.model_dump(exclude_unset=True)
                if nested is not None and not isinstance(nested, dict):
                    raise ValueError("style must be an object before merging misplaced fields")
                style = dict(nested or {})
                for key in moved:
                    if key in style and style[key] != value[key]:
                        raise ValueError(f"Conflicting {key!r} at layer and style levels; "
                                         f"provide only style.{key}")
                    style[key] = value[key]
                value = {key: item for key, item in value.items() if key not in moved}
                value["style"] = style
        result = handler(value)
        if moved:
            result._normalized_style_fields = moved
        return result

    @model_validator(mode="after")
    def validate_kind(self) -> QgisProjectLayer:
        if self.kind == "raster" and (
            self.style is not None or self.label_field or self.label_style or self.layer_name
            or self.derivation is not None
        ):
            raise ValueError("vector style/labels/layer_name do not apply to raster sources")
        if self.kind == "vector" and self.raster_style is not None:
            raise ValueError("raster_style applies only to raster sources")
        return self


class QgisExtent(BaseModel):
    """A positive rectangle in the project CRS, independent of any layer CRS."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    xmin: float = Field(description='范围最小X，单位为工程CRS单位；与xmax共同确定东西边界。')
    ymin: float = Field(description='范围最小Y，单位为工程CRS单位；与ymax共同确定南北边界。')
    xmax: float = Field(description='范围最大X，必须大于xmin；不是纸面mm。')
    ymax: float = Field(description='范围最大Y，必须大于ymin；不是纸面mm。')

    @model_validator(mode="after")
    def validate_area(self) -> QgisExtent:
        if self.xmin >= self.xmax or self.ymin >= self.ymax:
            raise ValueError("detail_extent must be xmin,ymin,xmax,ymax with positive area")
        return self


LayerReference = Annotated[int, Field(strict=True, ge=0)] | Annotated[str, Field(min_length=1)]


class QgisFrameRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    layer_index: LayerReference = Field(description=(
        "必填。主体图层，使用同一调用中的精确 layer_key，或 0 基源图层索引。"
        "图层顺序变化后不要沿用旧索引；优先用稳定 layer_key。"
    ))
    minimum_visible_features: int = Field(default=1, ge=0, description=(
        "可选，默认 1。该主体在此图框内至少可见的要素数量。"
        "数值应来自声明的业务对象，不得为了通过校验而调小。"
    ))
    coverage: Literal["intersects", "complete"] = Field(default="intersects", description=(
        "可选，默认 intersects。intersects = 至少一个要素与图框相交；"
        "complete = 所有选中几何都必须完整落在图框内。具名且不能裁切的主体用 complete。"
    ))
    key_field: str = Field(default="", description=(
        "可选。与 feature_keys 配对使用的业务键字段；有 feature_keys 时必填，"
        "且键在源中必须唯一、非空。"
    ))
    feature_keys: list[str | int | float] | None = Field(default=None, description=(
        "可选。此图框要核对的精确业务键值集合；用于具名子集。"
        "不要用行号、显示编号或猜测的标签列表代替真实键。"
    ))

    @model_validator(mode="after")
    def validate_keys(self):
        if self.feature_keys is not None and (not self.key_field or not self.feature_keys):
            raise ValueError("feature_keys requires key_field and a nonempty explicit selection")
        return self


class QgisMapFrameSpec(BaseModel):
    """Choose an extent's subject separately from the layers drawn in that frame."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    purpose: str = Field(default="", max_length=500, description=(
        "可选。内部说明此图框承担什么职责（总览/详图/名单等），不会绘制成可见文字。"
        "可见详图标题用 layout.detail_title。"
    ))
    requirements: list[QgisFrameRequirement] = Field(default_factory=list, max_length=64,
        description=(
            "可选。声明必须在此图框内保持可见的主体及覆盖要求；"
            "complete 表示选中几何不能裁切。layer_index 可写精确 layer_key。"
        ))

    label_layers: list[LayerReference] | None = Field(
        default=None, max_length=64, description=(
            "可选。允许在此图框内标注的图层。[] = 隐藏本页标注但保留符号；"
            "密集名称应交给详图或名单页。None = 沿用图层原有标注。"
        ))

    focus_layers: list[LayerReference] | None = Field(
        default=None, min_length=1, max_length=64,
        description=(
            "FIT 模式主体层：用于自动拟合图框范围的图层，排除背景层；优先写 layer_key。"
            "与显式 extent 互斥：主图用 map_extent，详图用 detail_extent 时不要再填本字段。"
        ),
    )
    render_layers: list[LayerReference] | None = Field(
        default=None, min_length=1, max_length=64,
        description=(
            "可选。此图框实际渲染的图层集合；必须包含 focus_layers 与 label_layers。"
            "工具会按原始从下到上顺序绘制，不会因列出的顺序改变叠放。"
        ),
    )
    padding_ratio: float | None = Field(
        default=None, ge=0, le=1,
        description=(
            "可选。FIT 主体范围每侧的对称留白比例；省略使用 cartography 配置。"
            "只影响拟合，不改变真实要素坐标。"
        ),
    )

    @model_validator(mode="after")
    def validate_selection(self) -> QgisMapFrameSpec:
        for name in ("focus_layers", "render_layers", "label_layers"):
            indices = getattr(self, name)
            if indices is not None:
                if len(indices) != len(set(indices)):
                    raise ValueError(f"{name} must not contain duplicate layer indices")
                setattr(self, name, sorted(indices,
                    key=lambda value: (isinstance(value, str), value)))
        if self.focus_layers and self.render_layers is not None and not (
            set(self.focus_layers) <= set(self.render_layers)
        ):
            raise PydanticCustomError("frame_selection",
                "focus_layers must be included in render_layers; missing={missing}",
                {"field": "focus_layers", "related_fields": ["focus_layers", "render_layers"],
                 "missing": sorted(set(self.focus_layers) - set(self.render_layers), key=str)})
        if self.label_layers and self.render_layers is not None and not (
            set(self.label_layers) <= set(self.render_layers)
        ):
            raise PydanticCustomError("frame_selection",
                "label_layers must be included in render_layers; missing={missing}",
                {"field": "label_layers", "related_fields": ["label_layers", "render_layers"],
                 "missing": sorted(set(self.label_layers) - set(self.render_layers), key=str)})
        return self



class QgisLayoutPlan(BaseModel):
    """An unchanged plan returned by native preflight, not a caller-authored layout."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = Field(description=(
        '原生预检返回的计划版本，必须原样传回，不手写。'
    ))
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$", description=(
        '输入/样式/字体的SHA256，数据改变应重新预检，不编辑摘要。'
    ))
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$", description=(
        '实测计划本身的SHA256，不截断、不重算、不挪用。'
    ))
    runtime_identity: dict[str, str] = Field(description=(
        '产生量测的QGIS/运行时版本，只作来源记录，不是可调参数。'
    ))
    font_identities: dict[str, dict[str, Any]] = Field(description=(
        '实测字体身份，改字体后量测失效，需重新预检。'
    ))
    source_identities: list[dict[str, Any]] = Field(default_factory=list, description=(
        '本计划实际量测的来源路径/摘要，不附加无关分析。'
    ))
    parameters: dict[str, Any] = Field(description=(
        '实测有效布局参数，作为计划整体原样传回。'
    ))
    regions: dict[str, list[int | float]] = Field(description=(
        '实测纸面区域坐标，单位以返回计划为准，不手动移动。'
    ))
    frames: dict[str, dict[str, Any]] = Field(description=(
        '实测图框参数和适配结果，随计划原样传回。'
    ))
    content_metrics: dict[str, Any] = Field(description=(
        '文字/表格等实测内容尺寸，不是用户设定。'
    ))
    adaptations: list[dict[str, Any]] = Field(description=(
        '原生预检已执行的适配记录，保留完整返回值。'
    ))


class QgisLabelTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_index: LayerReference = Field(description=(
        "必填。名单源图层：精确 layer_key 或同一调用中的 0 基索引。"
        "可用 role='table_only' 图层登记名单而不绘制；但不能把它放入图框渲染列表。"
    ))
    fields: list[str] = Field(min_length=2, max_length=8, description=(
        "必填。按显示顺序列出至少两个真实存在字段。"
        "键身份由 key_field 决定，省略时使用第一个字段；"
        "不要把排名/显示编号当键，除非它对完整源确实唯一且非空。"
    ))
    key_field: str = Field(default='', description=(
        "可选。源内唯一、非空的稳定业务键，用于绑定名单与地图符号；可不在 fields 中显示。"
        "首个显示字段是排名/编号且子集外为空时，必须显式提供 key_field。"
    ))
    headers: dict[str, str] = Field(default_factory=dict, max_length=8, description=(
        "可选。现有字段的显示表头，例如 {'name':'名称','distance_m':'距离（米）'}；"
        "键必须是 fields 中的真实字段，单位写在表头而非凭空推断。"
    ))
    title: str = Field(default='重点对象名单', max_length=100, description=(
        "可选。独立名单页的可见表题；空字符串表示显式关闭。"
    ))
    note: str = Field(default='编号对应地图或完整名册；距离等计量单位请在列标题注明。',
                      max_length=240, description=(
        "可选。名单页说明文字，不自动推断业务单位或质量结论。"
    ))
    page_columns: int | None = Field(default=None, ge=1, le=4, description=(
        "可选。独立名单页并排表格数量；空值沿用 columns。字段较多时建议 1。"
    ))
    columns: int = Field(default=3, ge=1, le=4, description=(
        "可选，默认 3。每页并排的完整表格数量，不是属性字段数；"
        "每个并行表格都重复全部字段和表头。"
    ))
    height_mm: float = Field(default=50, ge=15, le=400, description=(
        "可选。内联名单在页面中预留的高度（毫米）；独立名单页仅作规划提示。"
        "实际行数来自所有选中源记录，不能靠压高度丢行。"
    ))
    font_size_pt: float = Field(default=8, ge=5, le=20, description=(
        "可选。名单文字字号（pt）。缩小字号不能成为丢记录的理由；"
        "实测放不下时应分页或扩页。"
    ))
    placement: Literal["auto", "inline", "pages"] = Field(default="auto", description=(
        "可选，默认 auto。auto = 能放下就内联，溢出自适到独立可编辑页；"
        "pages = 强制独立名单页；inline = 只允许内联，放不下时按实测容量失败。"
        "任何模式都不能静默删除记录。"
    ))
    top_n: int | None = Field(default=None, ge=1, description=(
        "可选。按 sort_by 排序后显式取前 N 条；sort_by 必须同时提供。"
        "完整源名单仍保留为 CSV。省略表示包含全部记录。"
    ))
    sort_by: str = Field(default="", description=(
        "可选。真实字段名，用于稳定名单排序；选择 top_n 时必填。"
    ))
    descending: bool = Field(default=False, description=(
        "可选，默认 false。true 时反转 sort_by 顺序；只有与 top_n 结合才改变选集。"
    ))
    keys: list[str | int | float] | None = Field(default=None, min_length=1, description=(
        "可选。要包含的显式 key_field（或首字段）值集合；与 top_n 互斥。"
        "使用源业务键，不要传行号或猜测的显示编号。"
    ))
    field_widths: dict[str, float] = Field(default_factory=dict, description=(
        "可选。字段相对列宽，例如 {name:3, risk:1}；未列字段按 1。"
        "键必须是 fields 中的真实字段。"
    ))
    precision: dict[str, int] = Field(default_factory=dict, description=(
        "可选。按字段设显示小数位（0..10）；只影响显示，不改变源值或统计。"
    ))

    @model_validator(mode="after")
    def validate_selection(self):
        if self.top_n is not None and (not self.sort_by or self.keys is not None):
            raise ValueError("top_n requires sort_by and cannot be combined with keys")
        unknown = sorted(set(self.field_widths) - set(self.fields))
        if unknown:
            raise ValueError(f"field_widths has undisplayed fields {unknown}; "
                             "use only fields listed for display")
        unknown = sorted(set(self.headers) - set(self.fields))
        if unknown:
            raise ValueError(f"headers has undisplayed fields {unknown}")
        unknown = sorted(set(self.precision) - set(self.fields))
        if unknown:
            raise ValueError(f"precision has undisplayed fields {unknown}")
        if self.sort_by and not self.fields and not self.key_field:
            raise ValueError("sort_by needs at least one field or key_field for the source")
        return self



class QgisProjectLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail_title: str = Field(default="", description=(
        "可选。详图页/插图实际绘制的标题文字；用真实主体和范围表述。"
        "它是可见文字，不是 QgisMapFrameSpec.purpose（内部说明，不显示）。"
    ))
    name: str = Field(default="Map", description=(
        "可选。QGIS 打印布局名称，默认 Map；跨修订保持稳定，便于 export/delivery 找到同一布局。"
        "它不是地图标题。"
    ))
    width_mm: float = Field(default=297, ge=60, le=2000, description=(
        "可选。纸张宽度（毫米），默认 A4 横向 297。工具会实测页面内容；"
        "图例/名单/详图放不下时可能按规则扩页，不应靠裁掉主体或缩小文字硬塞。"
    ))
    height_mm: float = Field(default=210, ge=60, le=2000, description=(
        "可选。纸张高度（毫米），默认 A4 210。两侧内边距后仍须给主图至少 40mm；"
        "不要仅为让名单塞下而任意压低高度。"
    ))
    margin_mm: float = Field(default=12, ge=0, le=100, description=(
        "可选。统一页边距（毫米）。是否溢出由实际文字/图例/表格矩形测量决定，而不只是这个边距。"
    ))
    legend: bool = Field(default=True, description=(
        "可选，默认 true。是否绘制图例。仅在确实不需要图例时关闭；"
        "开启时图例类别/颜色必须与当前实际图层和分类一致。"
    ))
    scale_bar: bool = Field(default=True, description=(
        "可选，默认 true。是否绘制比例尺。比例尺从真实地图框和 CRS 计算，不沿用截图或旧脚本数值。"
    ))
    detail_extent: QgisExtent | None = Field(default=None, description=(
        "可选，显式详图范围（EXPLICIT 模式），单位是工程 crs。"
        "与 detail_frame.focus_layers 互斥，且要求已提供 crs。"
        "只给确实需要放大的局部范围，不要拿主图范围冒充详图。"
    ))
    main_frame: QgisMapFrameSpec | None = Field(default=None, description=(
        "可选。主图框的主体/渲染/标注契约。简单单图可省略；"
        "提供时 render_layers 必须包含 focus_layers 和 label_layers，且不能把 table_only 放进去。"
    ))
    detail_frame: QgisMapFrameSpec | None = Field(default=None, description=(
        "可选。详图框的主体/渲染/标注契约。FIT 模式用 focus_layers；"
        "EXPLICIT 模式改用 detail_extent。详图必须有明确的范围来源。"
    ))
    detail_placement: Literal["sidebar", "page"] = Field(default="sidebar", description=(
        "可选。sidebar = 详图与主图同页放在侧栏；page = 独立可编辑布局页并单独输出 PNG。"
        "侧栏会把主图挤到不可读时应选 page，而不是强行缩小文字。"
    ))
    label_table: QgisLabelTable | None = Field(default=None, description=(
        "可选。与同一源记录绑定的编号名单表。必须使用唯一非空键，不能静默丢行；"
        "内容溢出时按规则分页。"
    ))
    detail_minimum_zoom: float | None = Field(default=None, ge=1, le=20, description=(
        "可选。原生布局拟合后要求 main_scale/detail_scale >= 此值；真正放大详图时设 >1。"
        "不设表示允许定位图/旧式插图，不代表没有详图。"
    ))
    detail_zoom_policy: Literal["hard", "advisory"] = Field(default="hard", description=(
        "可选。hard = 强制执行明确放大约束；advisory = 只报告实测缩放，"
        "不因视觉建议放大纸张或裁切主体。"
    ))
    auto_expand_max_ratio: float | None = Field(default=None, ge=1, le=10, description=(
        "可选。相对请求纸张的最大实测扩页倍率。若内容在该上限内仍放不下，"
        "应以实测诊断失败，而不是裁切主体或删除图例/名单条目。"
    ))
    layout_plan: QgisLayoutPlan | None = Field(default=None, description=(
        "可选。preflight 返回且未变化的 layout_plan；来源/样式/字体/参数变化会被拒绝。"
    ))
    footer_text: str = Field(default="", description=(
        "可选。只放来源/上下文备注。经核实的统计应来自 analysis_manifests；"
        "分组时次面积/汇总可用 geo_analysis area。不要把自由文字当事实来源。"
    ))
    font_family: str = Field(default="Microsoft YaHei", description=(
        "可选。全部实测布局文字使用的已安装字体族；检查适配时保持稳定，换字体会改变真实文字宽度。"
    ))
    font_file: str = Field(default="", description=(
        "可选。workspace/data 读取根内的 TTF/TTC/OTF 文件。"
        "已安装字体优先用 font_family；系统目录中的显式路径不在授权读取范围。"
    ))
    title_font_size_pt: float = Field(default=18, ge=8, le=40, description=(
        "可选。地图标题字号（pt）。标题按预留标题带的实测宽度检查；"
        "不要靠缩边距掩盖溢出。"
    ))
    body_font_size_pt: float = Field(default=9, ge=5, le=20, description=(
        "可选。页脚/正文文字字号（pt），不控制名单表；名单字号用 QgisLabelTable font_size_pt。"
    ))
    legend_font_size_pt: float | None = Field(default=None, ge=5, le=20, description=(
        "可选。图例字号（pt）；省略用渲染默认。图例实际尺寸会被测量，不能假定放得下。"
    ))
    legend_auto_fit: bool = Field(default=True, description=(
        "可选，默认 true。联合测量图例/名单/页脚/详图缩放，必要时扩纸张或页面，"
        "但不裁切地理主体、不删除条目、不缩小文字。false 时保持固定图框拟合，需读取实测诊断。"
    ))
    detail_height_ratio: float = Field(default=0.4, ge=0.1, le=0.8, description=(
        "可选。侧栏详图占可用地图高度比例（0.1..0.8）。"
        "系统会实测图框，无法满足时报告约束而不是裁切。"
    ))
    sidebar_width_mm: float = Field(default=60, ge=20, le=500, description=(
        "可选。详图在主图旁时的侧栏宽度（毫米）。"
        "应保证主图与图例在实测比例下仍可读。"
    ))
    gap_mm: float = Field(default=5, ge=1, le=30, description=(
        "可选。主图、侧栏、页脚等面板之间的间距（毫米）。"
    ))
    title_height_mm: float = Field(default=16, ge=8, le=60, description=(
        "可选。顶部标题带高度（毫米）；标题文字按实际字体测量后必须放入该区域。"
    ))
    footer_height_mm: float = Field(default=18, ge=10, le=100, description=(
        "可选。底部页脚高度（毫米）；页脚、来源和统计文字按实测矩形检查。"
    ))

    @model_validator(mode="after")
    def validate_page(self) -> QgisProjectLayout:
        if min(self.width_mm, self.height_mm) - 2 * self.margin_mm < 40:
            raise ValueError("page dimensions must leave at least 40mm inside margins")
        sidebar = self.sidebar_width_mm + self.gap_mm if (
            self.legend or self.detail_extent is not None or self.detail_frame is not None
        ) else 0
        if self.width_mm - 2 * self.margin_mm - sidebar < 40:
            raise ValueError("page must leave at least 40mm for the main map beside the sidebar")
        if self.height_mm - 2 * self.margin_mm - self.footer_height_mm - self.gap_mm < 40:
            raise ValueError("page must leave at least 40mm for the main map above the footer")
        if self.detail_frame is not None:
            if self.detail_frame.focus_layers and self.detail_extent is not None:
                raise PydanticCustomError("frame_extent_conflict",
                    "detail_extent conflicts with detail_frame.focus_layers",
                    {"field": "detail_extent", "related_fields": [
                        "detail_extent", "detail_frame.focus_layers"]})
            if self.detail_extent is None and not self.detail_frame.focus_layers:
                raise PydanticCustomError("frame_extent_missing",
                    "detail_frame requires focus_layers or detail_extent",
                    {"field": "detail_frame", "related_fields": [
                        "detail_frame.focus_layers", "detail_extent"]})
        return self


class QgisSpecPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: list[str | Annotated[int, Field(strict=True, ge=0)]] = Field(min_length=1, description=(
        'JSON路径数组，例如["layers",0,"style","color"]；列表仅可在下一个索引追加。'
    ))
    operation: Literal["set", "remove"] = Field(default="set", description=(
        '默认set添加/替换值；remove删除对象字段，禁止删除列表索引导致身份偏移。'
    ))
    value: Any = Field(default=None, description=(
        'set使用且必须符合目标字段类型；remove忽略，默认null。'
    ))

class QgisStyleEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["style"] = Field(description='必填固定style，表示修改样式。')
    layer_key: str = Field(min_length=1, description=(
        'spec/inspect返回的稳定图层键，不是显示标题。'
    ))
    style: QgisVectorStyle = Field(description=(
        '该层替换样式对象；遵循编辑合并规则，颜色微调保留未指定的分类。'
    ))

class QgisLabelEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["labels"] = Field(description='必填固定labels，表示修改标注。')
    layer_key: str = Field(min_length=1, description=(
        'spec/inspect返回的准确图层键。'
    ))
    label_field: str = Field(min_length=1, description=(
        '必填，现存字段或派生编号字段，至少1字符，不是位置参数。'
    ))
    label_style: QgisLabelStyle | None = Field(default=None, description=(
        '可选标注样式，默认null保留现有值。'
    ))

class QgisFrameEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["frame"] = Field(description='必填固定frame，表示修改图框。')
    frame: Literal["main", "detail"] = Field(description=(
        '必填，main主图或detail详图。'
    ))
    settings: QgisMapFrameSpec = Field(description=(
        '必填图框对象；适配模式用focus_layers，显式extent与之互斥。'
    ))
    extent: QgisExtent | None = Field(default=None, description=(
        '可选工程CRS范围，默认null，与settings.focus_layers互斥。'
    ))
    minimum_zoom: float | None = Field(default=None, ge=1, le=20, description=(
        '可选实测的详图放大倍数阈值1..20，仅detail有效。'
    ))
    zoom_policy: Literal["hard", "advisory"] = Field(default="advisory", description=(
        '默认advisory仅提示比例风险；hard未满足量测约束时拒绝，不裁去声明对象。'
    ))

    @model_validator(mode="after")
    def validate_mode(self):
        if self.extent is not None and self.settings.focus_layers:
            raise ValueError("Choose extent or settings.focus_layers for this frame, never both")
        if self.frame == "main" and self.minimum_zoom is not None:
            raise ValueError("minimum_zoom applies to detail only")
        return self

class QgisLayerEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["layer"] = Field(description='必填固定layer，表示新增或替换图层。')
    layer: QgisProjectLayer = Field(description=(
        '完整图层对象；替换派生文件保留layer_key，避免后续编辑失去关联。'
    ))

class QgisRosterEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["roster"] = Field(description='必填固定roster，表示修改名单页。')
    table: QgisLabelTable | None = Field(default=None, description=(
        '名单定义，null移除；须使用真实字段和非空唯一键，不能静默截断。'
    ))

MapEdit = Annotated[QgisStyleEdit | QgisLabelEdit | QgisFrameEdit | QgisLayerEdit | QgisRosterEdit,
                    Field(discriminator="operation")]


class GeoQgisProjectInput(BaseModel):
    """Authoritative native QGIS project contract.

    Typical flow: help (optional) -> spec -> build(spec_ref, edits) -> optional
    inspect/preflight -> geo_map_review -> geo_deliver. Build renders a native
    preview; only delivery materializes the editable .qgz/.qgs package.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"action": "spec", "layers": [{
            "file_path": "ACTUAL_VECTOR_PATH", "name": "居民点",
            "style": {"renderer": "categorized", "field": "actual_class",
                      "categories": [{"value": "high", "label": "高风险", "color": "#d73027"}]},
            "label_field": "actual_id"}],
         "crs": "EPSG:4547", "title": "实际标题",
         "layout": {"width_mm": 297, "height_mm": 210}},
        {"action": "build", "spec_ref": "returned-qgis-spec-ref",
         "edits": [{"operation": "roster", "table": {
             "layer_index": "resident-layer-key", "fields": ["actual_id", "distance_m"],
             "key_field": "actual_id", "placement": "pages"}}]},
    ]})
    _edit_normalizations: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="wrap")
    @classmethod
    def normalize_edit_structure(cls, value, handler):
        from geoharness.qgis.specification import normalize_edit_structure

        normalized, notes = normalize_edit_structure(value)
        result = handler(normalized)
        result._edit_normalizations = notes
        return result

    action: Literal[
        "build", "create", "inspect", "export", "preflight", "spec", "help", "recover"
    ] = Field(
        description=(
            "必填。build = 在一个原生作业中应用 spec_ref+edits、验证并渲染预览，"
            "是正常出图的首选；spec = 保存/修改草案并返回 spec_ref；preflight = 只验证；"
            "create = 仅兼容旧冻结流程，只接受 action+plan_ref；"
            "inspect = 只读查看返回的 spec/plan/工程/地图台账；"
            "export = 从已有 .qgs/.qgz 渲染预览，不改原工程；"
            "help = 读取某一字段组的真实 schema；recover = 检查已登记 map_id 的台账。"
            "不要在本工具中使用 geo_qgis Processing 的 INPUT/OUTPUT 参数。"
        ))
    section: Literal["workflow", "layer", "roster", "style", "labels", "frame", "layout",
                     "edits"] = Field(default="workflow", description=(
        "仅 action=help 时有效。选择要读取的契约组：workflow 总流程、layer 图层、"
        "style 符号、labels 标注、frame 图框、layout 页面、roster 名单表、edits 增量编辑。"
        "不填时返回 workflow；其他 action 忽略该字段。"
    ))
    detail_path: list[str | int] = Field(default_factory=list, max_length=16, description=(
        "仅 help/inspect 的只读查看选择器。指向返回契约或已验证 spec/plan 内的 JSON 路径，"
        "例如 ['schema','properties','focus_layers']；不是文件系统路径。"
        "不要把它传给 spec/build/create/preflight/export。"
    ))
    offset: int = Field(default=0, ge=0, description=(
        "仅 help/inspect 的只读分页起点（字符偏移）。用于查看长诊断或大 schema；"
        "不要把它传到会修改/渲染的动作。"
    ))
    limit: int = Field(default=8, ge=1, le=32, description=(
        "仅 help/inspect 的单窗展示上限（条目/字符），不是图层数量、渲染上限或质量阈值。"
    ))
    history_offset: int | None = Field(default=None, ge=0, description=(
        "仅台账 inspect(map_id=...) 的分页锚点；必须原样使用返回 read_call 里的值。"
    ))
    history_limit: int | None = Field(default=None, ge=1, le=32, description=(
        "仅台账 inspect(map_id=...) 的分页窗口；必须原样使用返回 read_call 里的值。"
    ))
    edits: list[MapEdit] = Field(default_factory=list, max_length=32, description=(
        "可选。配合 action=spec/build 和已有 spec_ref 的类型化增量操作；"
        "operation 只能是 style/labels/frame/layer/roster，目标用实际返回的 layer_key。"
        "局部样式、标注、图框、名单问题只提交对应 edit，不要重写整个图层数组。"
        "示例：{operation:'style',layer_key:'actual-key',style:{...}}。"
    ))
    spec_ref: str = Field(default="", description=(
        "已有不可变 qgis-spec 引用，必须原样使用工具返回的 qgis-spec:...。"
        "action=spec 保存/编辑草案；action=build 应用 edits 并一次验证/导出；"
        "action=preflight 只检查；action=inspect 只读取。"
        "使用 spec_ref 时不要重复发送 layers/title/crs/layout 等已冻结字段。"
    ))
    patches: list[QgisSpecPatch] = Field(default_factory=list, max_length=64, description=(
        "高级低层草案补丁，仅在类型化 edits 不支持目标字段时使用。"
        "path 是实际 schema 键/下标的数组，例如 "
        "[{path:['layout','width_mm'],value:420}]；删除用 operation='remove'。"
        "列表追加必须使用准确的下一索引；不要使用 paper_size/page_width_mm 等臆造键。"
    ))
    plan_ref: str = Field(default="", description=(
        "旧版冻结创建返回的 plan_ref；只有 action=create 接受，且只传 action+plan_ref。"
        "map_id/title/output_format 等已包含在检查过的 plan 中，不能重复覆盖。"
    ))
    layers: list[QgisProjectLayer] = Field(
        default_factory=list,
        max_length=64,
        description=(
            "新建工程/草案的图层，按从下到上绘制顺序排列（背景在前、标注/编号在后）。"
            "spec/build/create/preflight 至少需要一个真实本地文件。"
            "file_path 必须是已检查到的实际路径；role='table_only' 的图层只用于名单，不参与绘制。"
            "每个 layer_key 是后续 edits 的稳定目标，不要用显示名称代替。"
        ),
    )
    project_path: str = Field(default="", description=(
        "已有本地 .qgs/.qgz 文件，仅 inspect/export 使用。"
        "inspect/export 会保留其图层、CRS、布局；不要同时传新的 layers/crs/layout。"
        "export 只产生预览与重建参数；可编辑快照在最终geo_deliver时生成。"
    ))
    title: str = Field(default="", description=(
        "地图上实际可见的标题文字，不是 layout.name、图层名或文件名；会按预留标题带测量。"
    ))
    crs: str = Field(default="", description=(
        "工程/显示 CRS authority 字符串，如 EPSG:4547。"
        "显式 map_extent 或 detail_extent 时必须提供。"
        "距离/面积/纸面几何优先使用米制投影 CRS；不要把显示 CRS 当作计算 CRS 的证明。"
    ))
    map_extent: list[float] | QgisExtent | None = Field(default=None, description=(
        "可选，主图显式范围 [xmin,ymin,xmax,ymax]，单位是 crs。"
        "与 layout.main_frame.focus_layers（FIT 模式）互斥，不能同时使用。"
        "范围必须有限、面积为正；QGIS 会保留声明的完整覆盖。"
    ))
    layout: QgisProjectLayout | None = Field(default=None, description=(
        "打印布局契约：纸张、图框职责、图例、名单、页脚等。简单图可省略。"
        "width_mm/height_mm 是毫米纸面尺寸；实际图例/文字/表格会被测量而不是假定放得下。"
        "detail_extent 与 detail_frame.focus_layers 二选一。"
    ))
    layout_name: str = Field(default="", description=(
        "仅 inspect/export 用于选择已保存的打印布局名称；工程只有一个布局时可省略。"
        "spec/build/create 不使用该字段。"
    ))
    output_format: Literal["png", "pdf"] = Field(default="png", description=(
        "预览格式：png 用于像素审查，pdf 用于打印/矢量核对；"
        "build/create/export不生成工程，geo_deliver才生成一次并复用。"
    ))
    output_path: str = Field(default="", description=(
        "可选。仅相对文件名，位于本次原生 RUN_DIR 内；省略由工具生成。"
        "绝不能传绝对路径或 RUN_DIR 之外的路径。"
    ))
    project_format: Literal["qgz", "qgs"] = Field(default="qgz", description=(
        "可编辑工程格式：qgz 为便携压缩工程，qgs 为未压缩工程；都是真实 QGIS 工程，"
        "最终交付时才使用该格式生成工程；预览阶段不写工程文件。"
    ))
    dpi: int = Field(default=150, ge=36, le=600, description=(
        "栅格预览 DPI。改变 DPI 会改变像素尺寸，不改变布局的毫米几何；"
        "判断是否放得下应使用返回的布局测量，而不是只看像素。"
    ))
    map_id: str = Field(default="", description=(
        "逻辑地图的稳定 ID，用于跨版本、跨工具复用同一张图；修复同一张图时保持它不变。"
        "返回的 operation_id 是证据身份，不是 map_id。"
    ))
    analysis_manifests: list[str] = Field(
        default_factory=list, max_length=8,
        description=(
            "可选。只绑定其原始输出/已验证 layer.derivation 真的被绘制到当前工程的分析 manifest；"
            "省略时从实际绘制路径推断。不要附上无关统计或 Python 重写副本。"
        ),
    )


    @model_validator(mode="before")
    @classmethod
    def compile_layer_references(cls, value):
        from geoharness.qgis.specification import compile_references

        if isinstance(value, dict) and isinstance(value.get("layers"), str):
            value = {**value, "layers": cls.normalize_complete_layers(value["layers"])}
        return compile_references(value)

    @field_validator("layers", mode="before")
    @classmethod
    def normalize_complete_layers(cls, value):
        if isinstance(value, str):
            def unique_object(pairs):
                result = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError(f"layers JSON contains duplicate key: {key}")
                    result[key] = item
                return result

            def reject_constant(constant):
                raise ValueError(f"layers JSON cannot contain {constant}")

            try:
                value = json.loads(value, object_pairs_hook=unique_object,
                                   parse_constant=reject_constant)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise ValueError("layers requires a complete JSON array of layer objects") from exc
            if not isinstance(value, list):
                raise ValueError("layers JSON must contain an array of layer objects")
        return value

    @model_validator(mode="after")
    def validate_action(self) -> GeoQgisProjectInput:
        selectors = {"detail_path", "offset", "limit"}
        if self.action == "recover" or (self.action == "inspect" and self.map_id and not (
                self.project_path or self.spec_ref or self.plan_ref)):
            allowed = {"action", "map_id", *(
                selectors | {'history_offset', 'history_limit'} if self.action == "inspect" else
                set())}
            if not self.map_id or self.model_fields_set - allowed:
                raise ValueError("Ledger inspect/recover accepts only map_id and read selectors")
            return self
        if self.history_offset is not None or self.history_limit is not None:
            raise ValueError('History anchors are only valid for ledger inspect with map_id')
        # A model_dump()/validated-spec round trip includes default selectors.
        # Only selectors that actually request a view are illegal on mutations.
        if (self.detail_path or self.offset != 0 or self.limit != 8) and self.action not in {
                "help", "inspect"}:
            raise PydanticCustomError("readonly_selectors",
                "Diagnostic selectors are read-only; use help or inspect",
                {"field": "detail_path" if self.detail_path else
                 "offset" if self.offset != 0 else "limit",
                 "related_fields": ["action", "detail_path", "offset", "limit"]})
        if self.action == "help":
            if self.model_fields_set - {"action", "section", *selectors}:
                raise ValueError("help accepts only action and section")
            return self
        if self.spec_ref:
            if self.action not in {"build", "spec", "preflight", "inspect"} or (
                    self.model_fields_set - {
                "action", "spec_ref", "patches", "edits", *(
                    selectors if self.action == "inspect" else set())
            }):
                raise ValueError("spec_ref accepts only action=build/spec/preflight/inspect, "
                                 "spec_ref and optional patches/edits")
            if self.action == "inspect" and (self.patches or self.edits):
                raise ValueError("inspect is read-only; use action=spec to edit")
            return self
        if self.patches or self.edits:
            raise ValueError("patches/edits require a returned spec_ref")
        if self.plan_ref:
            if self.action == "inspect" and not self.model_fields_set - {
                    "action", "plan_ref", *selectors}:
                return self
            if self.action != "create" or self.model_fields_set - {"action", "plan_ref"}:
                extra = sorted(self.model_fields_set - {"action", "plan_ref"})
                raise PydanticCustomError("frozen_plan_arguments",
                    "plan_ref create accepts only action and plan_ref; remove extra fields "
                    "{extra}; map_id/title/output_format are already in the checked plan. "
                    "Preflight changes first",
                    {"field": "plan_ref", "extra": extra,
                     "related_fields": ["action", "plan_ref", *extra]})
            return self
        if self.action == 'spec':
            # A draft can be assembled a layer at a time. Completeness and
            # cross-field constraints are enforced when it is preflighted.
            if self.project_path:
                raise ValueError('spec describes a new map; existing projects use inspect/export')
            return self
        if self.action in {"build", "create", "preflight", "spec"}:
            if not self.layers:
                raise ValueError("create requires at least one local layer")
            if not any(layer.role == "map" for layer in self.layers):
                raise ValueError("At least one map layer is required")
            if any(layer.role == "table_only" and layer.kind != "vector" for layer in self.layers):
                raise ValueError("table_only requires vector records")
            if self.project_path:
                raise ValueError("create cannot replace an existing project; use export")
        elif not self.project_path:
            raise ValueError("inspect/export requires project_path")
        elif self.layers or self.crs or self.map_extent is not None or self.layout is not None \
                or self.analysis_manifests:
            raise ValueError("inspect/export preserves the saved project's layers, CRS and layout")
        if self.map_extent is not None:
            if isinstance(self.map_extent, QgisExtent):
                self.map_extent = [self.map_extent.xmin, self.map_extent.ymin,
                                   self.map_extent.xmax, self.map_extent.ymax]
            if len(self.map_extent) != 4:
                raise ValueError("map_extent requires four coordinates")
            xmin, ymin, xmax, ymax = self.map_extent
            if not all(math.isfinite(value) for value in self.map_extent):
                raise ValueError("map_extent must contain finite coordinates")
            if xmin >= xmax or ymin >= ymax:
                raise ValueError("map_extent must be xmin,ymin,xmax,ymax with positive area")
            if not self.crs:
                raise ValueError("explicit map_extent requires crs")
        if self.layout and self.layout.detail_extent and not self.crs:
            raise ValueError("explicit detail_extent requires crs")
        if self.layout:
            table_indices = {i for i, layer in enumerate(self.layers) if layer.role == "table_only"}
            for frame in (self.layout.main_frame, self.layout.detail_frame):
                if frame and any(index in table_indices for index in [
                        *(frame.render_layers or []), *(frame.focus_layers or []),
                        *(frame.label_layers or []), *(r.layer_index for r in frame.requirements)]):
                    raise ValueError("table_only cannot be a rendered map subject")
            if self.map_extent is not None and self.layout.main_frame \
                    and self.layout.main_frame.focus_layers:
                raise ValueError("map_extent conflicts with main_frame.focus_layers")
            rendered = set()
            for name in ("main_frame", "detail_frame"):
                frame = getattr(self.layout, name)
                if name == "detail_frame" and frame is None and self.layout.detail_extent is None:
                    continue
                for indices in ((frame.focus_layers, frame.render_layers, frame.label_layers)
                                if frame else ()):
                    if indices is not None and any(index >= len(self.layers) for index in indices):
                        raise ValueError(f"{name} contains an out-of-range layer index")
                if frame:
                    rendered_here = (set(frame.render_layers) if frame.render_layers is not None
                                     else set(range(len(self.layers))))
                    for requirement in frame.requirements:
                        if requirement.layer_index not in rendered_here:
                            raise ValueError(f"{name} requirement references an unrendered layer")
                rendered.update(frame.render_layers if frame and frame.render_layers is not None
                                else range(len(self.layers)))
            if any(layer.role == "map" and layer.required and index not in rendered
                   for index, layer in enumerate(self.layers)):
                raise ValueError("Required layer is omitted from every map frame")
            if self.layout.label_table and self.layout.label_table.layer_index >= len(self.layers):
                raise ValueError("label_table references an unknown layer_index")
        return self


class GeoQgisProjectTool(BaseTool):
    name = "geo_qgis_project"
    description = (
        "QGIS 工程契约：读取/检查/导出现有工程，或在交付阶段生成最终可编辑工程。"
        "普通制图/修图/审图不得调用本工具创建布局预览；先用geo_cartography原生渲染PNG/PDF，"
        "再用geo_map_review读图。审图通过后geo_deliver一次生成可编辑工程并复用。"
        "一次保存 spec，再用 build(spec_ref, edits by layer_key) 在单个原生作业和单次模型"
        "往返中完成验证与导出。把相关图层/样式/标注/图框/名册编辑合并进该 build。"
        "用 help(section=style/labels/frame/layout/edits) 查看实际运行时 schema；不要使用 "
        "Processing help 或 geo_cartography 桥接字段。组合参数前先阅读始终可见的 help "
        "cautions。render_layers 必须包含 focus/label 图层；详图需要 focus_layers 或 "
        "detail_extent。诊断选择器只能用于 help/inspect。名册 keys 必须唯一且非空；top_n "
        "需要 sort_by 且会排除 keys。成功的 build 已经验证；不要重复 preflight/create。"
        "独立编号图层与引线同步生成；引线图层声明leader_binding，预检核对当前锚点与唯一ID。"
        "旧版 create 只接受 action 和返回的 plan_ref。高级完整输入和 path patches 仍受支持。"
        "图层从下到上绘制；原生样式使用 style.renderer/field/categories。每个图框声明 "
        "purpose 和必需对象覆盖；显式 extent 与 focus_layers 互斥。审查中的缩放偏好使用 "
        "advisory；保留所有声明的对象。使用 project_path 进行 inspect/export 会保留用户"
        "编辑；交付时写出可移植快照。使用原生分析输出/derivation 取得可验证来源。实际像素"
        "审查前输出保持 draft；最终交付使用 geo_deliver。"
    )
    input_model = GeoQgisProjectInput

    def to_api_schema(self):
        """Progressive discovery: exact full nested contracts remain in help."""
        from copy import deepcopy

        schema = super().to_api_schema()
        wire = deepcopy(schema['input_schema'])
        properties = wire['properties']
        for name, section in (('layers', 'layer'), ('layout', 'layout'),
                              ('edits', 'edits'), ('patches', 'edits')):
            original = properties[name]
            description = original['description'] + (
                f' 完整嵌套字段、类型、默认和示例使用help(section={section})按需读取；'
                '实际执行仍按完整严格Schema校验。')
            if name == 'layout':
                properties[name] = {'description': description, 'default': None,
                    'anyOf': [{'type': 'object', 'additionalProperties': True}, {'type': 'null'}]}
            else:
                properties[name] = {'type': 'array', 'description': description,
                    'items': {'type': 'object', 'additionalProperties': True},
                    **{key: original[key] for key in ('maxItems', 'default') if key in original}}
        # Inline the few remaining small definitions (e.g. named extents), then
        # discard the large unused style/layout tree from every model request.
        definitions = wire.pop('$defs', {})

        def inline(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    name = value['$ref'].rsplit('/', 1)[-1]
                    return {**inline(definitions[name]),
                            **{k: inline(v) for k, v in value.items() if k != '$ref'}}
                return {k: inline(v) for k, v in value.items()}
            return [inline(item) for item in value] if isinstance(value, list) else value

        schema['input_schema'] = inline(wire)
        return schema

    @guard_map_errors
    @guard_map_mutations
    async def execute(self, arguments: BaseModel, context: ToolExecutionContext,
                      *, _source_bindings: list[dict] | None = None,
                      _materialize: bool = False) -> ToolResult:
        assert isinstance(arguments, GeoQgisProjectInput)
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        input_normalizations = arguments._edit_normalizations
        if arguments.action == "recover" or (arguments.action == "inspect" and arguments.map_id
                and not (arguments.project_path or arguments.spec_ref or arguments.plan_ref)):
            from geoharness.cartography.jobs import inspect_map_ledger

            try:
                anchored = (arguments.history_offset is not None or
                            arguments.history_limit is not None)
                start = (arguments.history_offset or 0) if anchored else (
                    arguments.offset if not arguments.detail_path else 0)
                window = arguments.history_limit or arguments.limit
                payload = inspect_map_ledger(cwd=context.cwd, config=config,
                    map_id=arguments.map_id, recover=arguments.action == "recover",
                    offset=start, limit=window)
                visible = payload
                if (anchored or arguments.detail_path or
                        len(json.dumps(payload, ensure_ascii=False)) >
                        config.qgis.inspection_max_chars):
                    from geoharness.qgis.inspection import view

                    details = {key: value for key, value in payload.items() if key != "migration"}
                    visible = {key: payload[key] for key in (
                        "map_id", "status", "requires_recovery", "migration") if key in payload}
                    visible["diagnostic"] = view(details, call={"action": "inspect",
                        "map_id": arguments.map_id, "history_offset": start,
                        "history_limit": window}, path=arguments.detail_path,
                        offset=arguments.offset if anchored or arguments.detail_path else 0,
                        limit=arguments.limit, max_chars=config.qgis.inspection_max_chars - 600)
                return ToolResult(output=json.dumps(visible, ensure_ascii=False),
                                  metadata={"render_executed": False, **payload})
            except (OSError, ValueError) as exc:
                return ToolResult(output=f"Map ledger recovery/inspection failed: {exc}",
                                  is_error=True, metadata={"render_executed": False})
        if arguments.action == "help":
            from geoharness.qgis.inspection import help_output, view
            from geoharness.qgis.specification import contract_help

            payload = contract_help(arguments.section)
            call = {"action": "help", "section": arguments.section}
            try:
                visible = (view(payload, call=call, path=arguments.detail_path,
                    offset=arguments.offset, limit=arguments.limit,
                    max_chars=config.qgis.inspection_max_chars)
                    if arguments.detail_path or arguments.offset else
                    help_output(payload, call=call, max_chars=config.qgis.inspection_max_chars))
                return ToolResult(output=json.dumps(visible, ensure_ascii=False),
                                  metadata={"render_executed": False})
            except ValueError as exc:
                return ToolResult(output=str(exc), is_error=True,
                                  metadata={"render_executed": False})
        spec_reference = ""
        if arguments.action == "spec" or arguments.spec_ref:
            from geoharness.qgis.specification import (
                apply_edits,
                canonical_spec,
                load_spec,
                patch_spec,
                save_spec,
            )

            try:
                parameters = (load_spec(arguments.spec_ref, cwd=context.cwd, settings=config.qgis)
                    if arguments.spec_ref else arguments.model_dump(exclude={
                        "action", "plan_ref", "spec_ref", "patches", "edits", "section"},
                        exclude_unset=True))
                if arguments.action == "inspect":
                    from geoharness.qgis.inspection import view
                    from geoharness.qgis.specification import describe_layers

                    packet = {"status": "draft_inspected",
                        "spec_ref": arguments.spec_ref, "parameters": parameters,
                        "layers": describe_layers(parameters, context=context, config=config)}
                    if parameters.get("map_id"):
                        from geoharness.cartography.jobs import map_mutation_state

                        try:
                            packet["mutation_state"] = map_mutation_state(context.metadata,
                                cwd=context.cwd, config=config, map_id=parameters["map_id"])
                        except ValueError:
                            pass  # Legacy caller-chosen aliases are not ledger UUIDs.
                    if (arguments.detail_path or arguments.offset
                            or len(json.dumps(packet, ensure_ascii=False)) >
                            config.qgis.inspection_max_chars):
                        packet = {"spec_ref": arguments.spec_ref, **view(packet,
                            call={"action": "inspect", "spec_ref": arguments.spec_ref},
                            path=arguments.detail_path, offset=arguments.offset,
                            limit=arguments.limit,
                            max_chars=config.qgis.inspection_max_chars - 100)}
                    return ToolResult(output=json.dumps(packet,
                        ensure_ascii=False), metadata={"render_executed": False})
                parameters = patch_spec(parameters,
                    [patch.model_dump() for patch in arguments.patches])
                parameters = apply_edits(parameters,
                    [edit.model_dump(exclude_unset=True) for edit in arguments.edits])
                checked = GeoQgisProjectInput.model_validate({**parameters,
                    "action": ("create" if arguments.action == "build" else
                               "spec" if arguments.action == "spec" else "preflight")})
                parameters = checked.model_dump(
                    exclude={"action", "plan_ref", "spec_ref", "patches", "edits", "section"},
                    exclude_unset=True)
                parameters.setdefault('layers', [])
                reference, path = save_spec(parameters, cwd=context.cwd, settings=config.qgis)
                if arguments.action != "inspect":
                    from geoharness.cartography.jobs import record_map_draft
                    from geoharness.cartography.mutation_control import current_owner

                    owner = current_owner()
                    if owner:
                        record_map_draft(owner.map_id, context.metadata, cwd=context.cwd,
                                         config=config, spec_ref=reference)
                spec_reference = reference
                if arguments.action == "spec":
                    return ToolResult(output=json.dumps({"status": "draft_saved",
                        "spec_ref": reference, "spec_path": path,
                        "input_normalizations": input_normalizations,
                        "inspect_call": {"action": "inspect", "spec_ref": reference},
                        "layers": [{"layer_index": index, "layer_key": layer.get("layer_key"),
                                    "name": layer.get("name", "")}
                                   for index, layer in enumerate(
                                       canonical_spec(parameters).get("layers", []))],
                        "preflight_call": {"action": "preflight", "spec_ref": reference},
                        "build_call": {"action": "build", "spec_ref": reference},
                        "note": "Draft schema checked; data/native layout have not been checked."},
                        ensure_ascii=False), metadata={"spec_ref": reference,
                                                       "render_executed": False})
                arguments = checked
            except (OSError, ValueError) as exc:
                return ToolResult(output=f"Error: map specification failed: {exc}", is_error=True,
                                  metadata={"deterministic_error": True, "render_executed": False})
        if arguments.action == "build":
            arguments = arguments.model_copy(update={"action": "create"})
        if arguments.plan_ref:
            from geoharness.qgis.prepared import load_preflight

            try:
                packet = load_preflight(arguments.plan_ref, cwd=context.cwd, settings=config.qgis)
                if arguments.action == "inspect":
                    from geoharness.qgis.inspection import view

                    visible = view(packet,
                        call={"action": "inspect", "plan_ref": arguments.plan_ref},
                        path=arguments.detail_path, offset=arguments.offset,
                        limit=arguments.limit, max_chars=config.qgis.inspection_max_chars - 100)
                    return ToolResult(output=json.dumps({"plan_ref": arguments.plan_ref,
                        **visible}, ensure_ascii=False), metadata={"render_executed": False})
                arguments = GeoQgisProjectInput.model_validate({**packet["parameters"],
                                                                "action": "create"})
            except (OSError, ValueError) as exc:
                return ToolResult(output=f"Error: prepared preflight failed: {exc}", is_error=True,
                                  metadata={"deterministic_error": True, "render_executed": False})
        from geoharness.data.task_context import data_ref_snapshot, resolve_registered_path

        try:
            layers, bound_manifests = [], []
            references = (data_ref_snapshot(context.metadata, cwd=Path(context.cwd), config=config)
                          if any(layer.file_path.startswith("data:") for layer in arguments.layers)
                          else {})
            for layer in arguments.layers:
                path = resolve_registered_path(layer.file_path, context.metadata,
                                               cwd=Path(context.cwd), config=config)
                from geoharness.data.artifacts import assert_usable_artifact

                source_path = Path(path.split("|", 1)[0])
                if not source_path.is_absolute():
                    source_path = Path(context.cwd) / source_path
                assert_usable_artifact(source_path)
                record = references.get(layer.file_path, {})
                if record.get("producer") in {"geo_analysis", "geo_accessibility"} \
                        and record.get("analysis_manifest"):
                    bound_manifests.append(record["analysis_manifest"])
                layers.append(layer.model_copy(update={"file_path": path}))
            arguments = arguments.model_copy(update={"layers": layers,
                "analysis_manifests": list(dict.fromkeys([
                    *arguments.analysis_manifests, *bound_manifests]))})
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Error: data reference failed: {exc}", is_error=True,
                              metadata={"render_executed": False})
        if not config.qgis.enabled and arguments.action != "preflight":
            return ToolResult(output="Error: QGIS integration is disabled.", is_error=True)
        if arguments.action == "create" or (
            arguments.action == "preflight" and arguments.layout is not None and config.qgis.enabled
        ):
            # Inputs remain alive through worker shutdown, including cancellation.
            with TemporaryDirectory(prefix="geoh-qgis-inputs-") as directory:
                result = await self._execute(arguments, context, Path(directory).resolve(),
                                             source_bindings=_source_bindings,
                                             spec_reference=spec_reference,
                                             materialize=_materialize)
        else:
            result = await self._execute(arguments, context, source_bindings=_source_bindings,
                                         spec_reference=spec_reference,
                                         materialize=_materialize)
        if spec_reference and not result.is_error:
            raw = result.output.lstrip()
            visible, end = json.JSONDecoder().raw_decode(raw)
            visible["spec_ref"] = spec_reference
            if input_normalizations:
                visible["input_normalizations"] = input_normalizations
            result = ToolResult(output=json.dumps(visible, ensure_ascii=False) + raw[end:],
                                is_error=result.is_error,
                                metadata={**result.metadata, 'spec_ref': spec_reference})
        elif spec_reference and result.is_error:
            # Candidate edits are bound to the exact immutable draft that failed, never
            # silently retargeted to a newer source/layout revision.
            candidates = (result.metadata.get("diagnostics") or {}).get("candidates", [])
            calls = [{"strategy": item["strategy"], "base_spec_ref": spec_reference,
                      "base_input_digest": item["base_input_digest"],
                      "measured_zoom": item.get("actual_zoom"),
                      "edit_call": {"action": "spec", "spec_ref": spec_reference,
                                    "patches": item["patches"]}}
                     for item in candidates
                     if item.get("validation_status") == "native_preflight_passed"]
            recovery = {"spec_ref": spec_reference,
                        "inspect_call": {"action": "inspect", "spec_ref": spec_reference},
                        "candidate_edit_calls": calls,
                        "note": "Edits target this immutable spec only; preflight the new spec "
                                "and use its returned create_call. No candidate was applied."}
            result = ToolResult(output=result.output + "\n" + json.dumps(recovery,
                ensure_ascii=False), is_error=True,
                metadata={**result.metadata, **recovery})
        return result

    async def _execute(self, arguments, context, snapshot_directory=None,
                       *, source_bindings=None, materialize=False, spec_reference='') -> ToolResult:
        from geoharness.cartography.mutation_control import current_owner

        owner = current_owner()
        if owner:
            owner.stage("data_preflight" if arguments.action != "export" else "native_export")
        config = context.metadata["geoharness_config"]
        requested = arguments.model_dump(exclude={"action", "plan_ref"}, exclude_unset=True)
        preflight = None
        if arguments.action in {"create", "preflight"}:
            try:
                if snapshot_directory is not None:
                    pending = asyncio.create_task(asyncio.to_thread(
                        self._freeze_and_preflight, arguments, context, snapshot_directory,
                        **({"source_bindings": source_bindings} if source_bindings is not None
                           else {}),
                    ))
                    try:
                        arguments, preflight = await asyncio.wait_for(
                            asyncio.shield(pending), timeout=config.qgis.timeout_seconds,
                        )
                    except (TimeoutError, asyncio.CancelledError):
                        # Cancelling to_thread cannot stop native file reads/writes.
                        await _drain_task(pending)
                        raise
                else:
                    preflight = await asyncio.wait_for(
                        asyncio.to_thread(self._preflight, arguments, context),
                        timeout=config.qgis.timeout_seconds,
                    )
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                from geoharness.qgis.prepared import failure_summary

                failed = {"status": "failed", "error_summary": getattr(exc, "summary", str(exc)),
                          **({"diagnostics": exc.diagnostics}
                             if hasattr(exc, "diagnostics") else {})}
                return ToolResult(output="Error: QGIS map preflight failed: "
                                  + json.dumps(failure_summary(failed), ensure_ascii=False),
                                  is_error=True,
                                  metadata={"preflight_status": "failed", "render_executed": False,
                                            "deterministic_error": isinstance(exc, ValueError),
                                            **failed})
        from geoharness.qgis.reuse import recall_render, remember_render, render_key
        from geoharness.qgis.runner import execute_qgis

        if preflight is not None:
            preflight["parameter_normalizations"] = [
                {"layer_index": index, "moved_to_style": list(layer._normalized_style_fields)}
                for index, layer in enumerate(arguments.layers) if layer._normalized_style_fields
            ]
        payload = arguments.model_dump(exclude={"action"}, exclude_none=True)
        if arguments.action in {'create', 'export'}:
            # Only the trusted delivery caller can request a portable project.
            # This flag is deliberately absent from the model-visible input schema.
            payload['_preview_only'] = not materialize
        if preflight:
            creation = requested
            if not creation.get("crs"):
                creation["crs"] = preflight["display_crs"]
            if creation.get("layout"):
                creation["layout"].pop("layout_plan", None)
            # Freezing copies sources into a temporary directory. The creation
            # contract must retain the real business locators, otherwise a later
            # delivery materialization points at deleted temporary files.
            bindings = preflight.get("source_bindings", [])
            for layer, binding in zip(creation.get("layers", []), bindings):
                if isinstance(layer, dict) and isinstance(binding.get("file_path"), str):
                    layer["file_path"] = binding["file_path"]
                if isinstance(layer, dict) and binding.get('layer_key'):
                    layer['layer_key'] = binding['layer_key']
                if (isinstance(layer, dict) and isinstance(binding.get("source_layer"), str)
                        and binding["source_layer"]):
                    layer["layer_name"] = binding["source_layer"]
                elif isinstance(layer, dict):
                    # Freezing names a temporary single-layer GPKG. Do not carry
                    # that synthetic layer name back to the original source.
                    layer.pop("layer_name", None)
            payload["_creation_spec"] = {
                "spec_ref": spec_reference,
                "parameters": creation,
                "source_bindings": bindings,
                "source_inventory": preflight.get('source_inventory', {}),
                "analysis_manifests": preflight.get("analysis_manifests", []),
            }
        if preflight:
            payload.update(crs=preflight["display_crs"], map_extent=preflight["map_extent"])
            payload["_explicit_map_extent"] = preflight["map_extent_source"] == "explicit"
            payload["_layout_sources"] = preflight.get("layout_sources", [])
            payload["_extent_policy"] = {
                key: getattr(config.cartography, key) for key in (
                    "minimum_span_geographic", "minimum_span_projected", "extent_padding")
            }
            facts = preflight.get("analysis") or {
                "status": "none", "analyses": [], "footer_text": ""}
            if facts.get("status") in {"verified", "none"}:
                from geoharness.cartography.provenance import apply_analysis_footer

                try:
                    payload = apply_analysis_footer(payload, facts)
                except ValueError as exc:
                    return ToolResult(output=f"Error: analysis footer layout invalid: {exc}",
                                      is_error=True, metadata={"deterministic_error": True})
        # NULL is a real category key, and must participate in the stable style binding.
        for source, packed in zip(arguments.layers, payload.get("layers", [])):
            if not source.required and "minimum_visible_features" not in source.model_fields_set:
                packed["minimum_visible_features"] = 0
            if source.style is not None:
                for category, encoded in zip(source.style.categories,
                                             packed.get("style", {}).get("categories", [])):
                    encoded["value"] = category.value
        plan = (payload.get("layout") or {}).get("layout_plan")
        if plan is not None:
            from geoharness.qgis.project_worker import _layout_input_digest

            if plan["input_digest"] != _layout_input_digest(payload):
                return ToolResult(output="Error: layout_plan is stale: sources/styles/parameters "
                    "changed; run preflight again", is_error=True,
                    metadata={"deterministic_error": True, "render_executed": False})
        reuse_key = render_key(payload, config) if arguments.action == "create" else None
        current_version = None
        if reuse_key and owner:
            from geoharness.cartography.jobs import map_mutation_state

            current_version = map_mutation_state(context.metadata, cwd=context.cwd,
                config=config, map_id=owner.map_id)['latest_version']
        reused = recall_render(context.metadata, reuse_key, current_version=current_version)
        if reused is not None:
            return reused
        if arguments.action == "preflight":
            if arguments.layout is not None and config.qgis.enabled:
                try:
                    if owner:
                        owner.stage("native_preflight")
                    result = await execute_qgis(
                        "project_preflight", payload, cwd=Path(context.cwd), settings=config.qgis,
                        read_roots=[*qgis_read_roots(config, Path(context.cwd)),
                                    *([snapshot_directory]
                                      if snapshot_directory is not None else [])],
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    return ToolResult(output=f"Error: QGIS layout preflight failed: {exc}",
                                      is_error=True, metadata={"render_executed": False})
                if result.get("status") != "succeeded":
                    from geoharness.qgis.prepared import failure_summary

                    return ToolResult(output="Error: QGIS layout preflight failed: "
                                      + json.dumps(failure_summary(result), ensure_ascii=False),
                                      is_error=True, metadata={**result, "render_executed": False,
                                      "deterministic_error": (result.get("error_summary") or "")
                                      .startswith("ValueError:")})
                _apply_native_frame_evidence(preflight, result["result"])
            elif arguments.layout is not None:
                preflight["layout"] = {"bounds_check": {"checked": False, "valid": None},
                                       "reason": "QGIS is disabled"}
            from geoharness.qgis.prepared import save_preflight, summary

            if preflight.get("layout_plan"):
                requested.setdefault("layout", {})["layout_plan"] = preflight["layout_plan"]
            try:
                from geoharness.cartography.mutation_control import current_owner, plan_binding

                owner = current_owner()
                binding = (plan_binding(context, map_id=owner.map_id)
                           if owner is not None else None)
                reference, report_path = save_preflight(requested, preflight, cwd=context.cwd,
                                                        settings=config.qgis, binding=binding)
            except (OSError, ValueError) as exc:
                return ToolResult(output=f"Error: QGIS preflight report could not be saved: {exc}",
                    is_error=True, metadata={"render_executed": False,
                                            "preflight_status": "failed"})
            # Keep small legacy reports readable inline; large reports use an exact capsule.
            visible = summary(preflight, reference, report_path,
                              max_chars=config.qgis.inspection_max_chars - 120)
            return ToolResult(output=json.dumps(visible, ensure_ascii=False),
                metadata={"preflight": preflight, "render_executed": False,
                          "plan_ref": reference, "report_path": str(report_path)})
        identity = arguments.map_id or map_identity(
            arguments.title or Path(arguments.project_path).stem or "qgis-map"
        )
        reserved = arguments.action in {"create", "export"} and not materialize
        if reserved:
            from geoharness.cartography.tracking import bind_map_job

            try:
                project_path = Path(arguments.project_path) if arguments.project_path else None
                saved_path = project_path
                if saved_path is not None and not saved_path.is_absolute():
                    saved_path = Path(context.cwd) / saved_path
                if arguments.action == "export" and not arguments.map_id \
                        and saved_path and saved_path.is_file():
                    from geoharness.cartography.jobs import saved_project_job

                    identity = await asyncio.to_thread(
                        saved_project_job, context.metadata, cwd=context.cwd, config=config,
                        project_path=project_path,
                    ) or identity
                identity = bind_map_job(context, identity,
                                        project_path)
            except (OSError, ValueError) as exc:
                return ToolResult(output=f"Error: map task could not start: {exc}", is_error=True)
            reserve_map_render(identity, context.metadata, config)
        payload["map_id"] = identity
        payload['_page_reuse'] = list(context.metadata.get('geo_page_reuse', {}).get(
            identity, {}).values())
        try:
            if owner:
                owner.stage("native_" + arguments.action)
            execution = execute_qgis(
                f"project_{arguments.action}",
                payload,
                cwd=Path(context.cwd),
                settings=config.qgis,
                read_roots=[*qgis_read_roots(config, Path(context.cwd)),
                            *([snapshot_directory] if snapshot_directory is not None else [])],
            )
            if snapshot_directory is not None:
                pending = asyncio.create_task(execution)
                try:
                    result = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    pending.cancel()
                    await _drain_task(pending)
                    raise
            else:
                result = await execution
        except (OSError, ValueError, RuntimeError) as exc:
            if reserved:
                refund_map_render(identity, context.metadata)
            return ToolResult(
                output=f"Error: QGIS project task could not run: {exc}", is_error=True
            )
        except asyncio.CancelledError:
            if reserved:
                refund_map_render(identity, context.metadata)
            raise
        if result.get("status") != "succeeded" and reserved:
            refund_map_render(identity, context.metadata)
        details: dict[str, Any] = result.get("result") or {}
        if result.get('status') == 'succeeded' and arguments.action in {'create', 'export'}:
            required = ['project_dir', 'project_manifest', 'preview_path']
            if materialize:
                required.append('project_file')
            missing = [key for key in required if not isinstance(details.get(key), str)
                       or not details[key]]
            if missing:
                result = {**result, 'status': 'failed', 'error_summary':
                    'NativeOutputContractError: native renderer returned no ' + ', '.join(missing)}
                if reserved:
                    refund_map_render(identity, context.metadata)
        if preflight and details.get("layout_plan"):
            _apply_native_frame_evidence(preflight, details)
        metadata = {**details, **result, "map_id": identity, "preflight": preflight,
                    "deterministic_error": (result.get("error_summary") or "")
                    .startswith("ValueError:"),
                    "render_executed": result.get("status") == "succeeded" and reserved}
        if metadata.get("project_file"):
            metadata["qgis_project"] = metadata["project_file"]
        from geoharness.qgis.prepared import execution_summary

        response = ToolResult(
            output=json.dumps(execution_summary(result), ensure_ascii=False, indent=2),
            is_error=result.get("status") != "succeeded",
            metadata=metadata,
        )
        if reserved and result.get("status") == "succeeded":
            from geoharness.cartography.tracking import track_render_result

            analysis = (preflight or {}).get("analysis")
            registration = asyncio.create_task(asyncio.to_thread(
                track_render_result, response, context, identity,
                preflight=preflight, analysis=analysis))
            try:
                tracked = await asyncio.shield(registration)
                remember_render(context.metadata, reuse_key, tracked,
                                limit=config.qgis.reuse_cache_entries)
                from geoharness.qgis.page_cache import remember_pages

                remember_pages(context.metadata, tracked, identity)
                return tracked
            except asyncio.CancelledError:
                await _drain_task(registration)
                raise
        return response

    def is_read_only(self, arguments: BaseModel) -> bool:
        return isinstance(arguments, GeoQgisProjectInput) and arguments.action in {
            "inspect", "preflight", "help",
        }

    def _freeze_and_preflight(self, arguments, context, directory, source_bindings=None):
        snapshot = freeze_qgis_inputs(
            [layer.model_dump(exclude_none=True) for layer in arguments.layers],
            directory=directory, cwd=Path(context.cwd),
            read_roots=qgis_read_roots(context.metadata["geoharness_config"], Path(context.cwd)),
        )
        if source_bindings is not None:
            if len(source_bindings) != len(snapshot.original_sources):
                raise ValueError("Map bridge source bindings must match actual layers")
            for original, binding in zip(snapshot.original_sources, source_bindings):
                primary_key = original.get("snapshot_primary_key")
                business_fields = original.get("business_fields")
                original.update(binding)
                if primary_key is not None:
                    original.update(snapshot_primary_key=primary_key,
                                    business_fields=business_fields)
            for source, binding in zip(snapshot.layout_sources, source_bindings):
                source.update(source=binding.get("file_path", source["source"]),
                              source_layer=binding.get('source_layer') or '',
                              derivation=binding.get("derivation"),
                              layer_key=binding.get("layer_key", source["layer_key"]))
        for original, layer in zip(snapshot.original_sources, arguments.layers):
            if "derivation" not in original and layer.derivation is not None:
                original["derivation"] = layer.derivation.model_dump(exclude_none=True)
        frozen = arguments.model_copy(update={
            "layers": [layer.model_copy(update={**updates, "layer_key": original["layer_key"]})
                       for layer, updates, original in zip(arguments.layers, snapshot.layers,
                                                          snapshot.original_sources)],
        })
        from geoharness.cartography.values import TEMPORAL_FIELDS, normalize_style

        for layer, original in zip(frozen.layers, snapshot.original_sources):
            if layer.style and original.get("temporal_fields"):
                import geopandas as gpd

                frame = gpd.read_file(layer.file_path, layer=layer.layer_name)
                frame.attrs[TEMPORAL_FIELDS] = original["temporal_fields"]
                layer.style = type(layer.style).model_validate(
                    normalize_style(frame, layer.style.model_dump())
                )
        report = self._preflight(frozen, context, snapshot=snapshot)
        report["layout_sources"] = snapshot.layout_sources
        report["source_bindings"] = snapshot.original_sources
        report['source_inventory'] = {str(path): digest
                                      for path, digest in snapshot.source_digests.items()}
        if source_bindings is not None:
            # Temporary bridge GPKGs are not the persistent business locators.
            # Bind the real originals (or explicitly retained inline inputs).
            from geoharness.data.artifacts import sha256_file

            report['source_inventory'] = {}
            for binding in source_bindings:
                path = Path(binding['file_path'])
                companions = [path]
                if path.suffix.lower() == '.shp':
                    companions.extend(p for p in path.parent.glob(path.stem + '.*')
                                      if p.suffix.lower() in {'.dbf', '.shx', '.prj', '.cpg'})
                report['source_inventory'].update({str(p): sha256_file(p) for p in companions})
        # Analysis evidence was checked at original locators, against frozen GDFs.
        # Sources may change after this point: the worker only uses the snapshots.
        snapshot.verify_sources()
        return frozen, report

    def _preflight(self, arguments, context, *, snapshot: FrozenQgisInputs | None = None) -> dict:
        import geopandas as gpd
        import rasterio

        from geoharness.cartography.preflight import validate_frames
        from geoharness.qgis.project_worker import _local_source
        from geoharness.qgis.worker_context import WorkerContext

        config = context.metadata["geoharness_config"]
        guard = WorkerContext(Path(context.cwd), Path(context.cwd),
                              [*qgis_read_roots(config, Path(context.cwd)),
                               *([snapshot.directory] if snapshot else [])])
        frames = []
        mapped_sources = []
        for index, layer in enumerate(arguments.layers):
            path, options = _local_source(layer.file_path, guard)
            common = {"label": layer.name or path.stem, "required": layer.required,
                      "role": layer.role,
                      "expected_feature_count": layer.expected_feature_count,
                      "minimum_visible_features": layer.minimum_visible_features if layer.required
                      else (layer.minimum_visible_features
                            if "minimum_visible_features" in layer.model_fields_set else 0)}
            if layer.kind == "raster":
                if layer.raster_style:
                    with rasterio.open(path) as source:
                        if layer.raster_style.band > source.count:
                            raise ValueError(f"Raster band {layer.raster_style.band} is missing")
                frames.append({**common, "raster_path": str(path)})
            else:
                name: str | int = layer.layer_name
                if not name and options:
                    selector, selected = options.split("=", 1)
                    name = int(selected) if selector == "layerid" else selected
                gdf = gpd.read_file(path, **({"layer": name} if name else {}))
                if layer.derivation is not None and snapshot is None:
                    gdf = derive_frame(gdf, layer.derivation)
                if layer.label_field and layer.label_style:
                    placement = layer.label_style.placement
                    types = set(gdf.geom_type.dropna())
                    allowed = ({"Point", "MultiPoint"} if placement in {
                        "around_point", "over_point"} else {"LineString", "MultiLineString"}
                        if placement == "parallel" else None)
                    if allowed is not None and types and not types <= allowed:
                        raise ValueError(f"layers[{index}].label_style.placement={placement!r} "
                            f"requires {sorted(allowed)}, got {sorted(types)} on "
                            f"{common['label']!r}; use placement='auto' "
                            "or 'horizontal' for polygons")
                if snapshot:
                    from geoharness.cartography.values import TEMPORAL_FIELDS

                    gdf.attrs[TEMPORAL_FIELDS] = snapshot.original_sources[index].get(
                        "temporal_fields", []
                    )
                frames.append({**common, "gdf": gdf, "label_field": layer.label_field,
                               "style": layer.style.model_dump() if layer.style else {},
                               "label_style": (layer.label_style.model_dump()
                                               if layer.label_style else {})})
                original_path = snapshot.original_sources[index]["file_path"] if snapshot else (
                    str(path)
                )
                derivation = layer.derivation.model_dump() if layer.derivation is not None else None
                if snapshot and "derivation" in snapshot.original_sources[index]:
                    derivation = snapshot.original_sources[index]["derivation"]
                mapped_sources.append({"file_path": original_path, "gdf": gdf,
                                       "label": common["label"],
                                       "layer_index": index,
                                       "derivation": derivation})
        leader_checks = []
        for index, layer in enumerate(arguments.layers):
            binding = layer.leader_binding
            if binding is None:
                continue
            from geoharness.execution.contracts import check_leader_lines

            indices = [binding.source_layer, binding.labels_layer, index]
            if any(type(i) is not int or i >= len(frames) or "gdf" not in frames[i]
                   for i in indices):
                raise ValueError('leader_binding requires three exact vector layer references')
            points, labels, lines = [frames[i]["gdf"] for i in indices]
            key = binding.key_field
            if key not in labels or key not in points:
                raise ValueError(f'leader_binding key {key!r} missing from source/anchor layers')
            selected = points[points[key].isin(labels[key])]
            checked = check_leader_lines(selected, labels, lines, key=key,
                metric_crs=binding.metric_crs, tolerance_m=binding.tolerance_m,
                source_gap_m=binding.source_gap_m, target_gap_m=binding.target_gap_m)
            if checked['status'] != 'passed':
                raise ValueError('Leader geometry does not match current source/anchors: '
                                 + json.dumps(checked, ensure_ascii=False))
            leader_checks.append({"layer_index": index, **checked})
        settings = config.cartography
        table = arguments.layout.label_table if arguments.layout else None
        if table:
            frame = frames[table.layer_index].get("gdf")
            if frame is None or len(set(table.fields)) != len(table.fields) or any(
                field not in frame.columns or field == frame.geometry.name for field in table.fields
            ):
                raise ValueError("label_table requires distinct existing vector attributes")
            key_field = table.key_field or table.fields[0]
            if key_field not in frame.columns or key_field == frame.geometry.name:
                raise ValueError(f"label_table key_field {key_field!r} is not an existing "
                                 "vector attribute")
            key = frame[key_field]
            if key.isna().any() or key.duplicated().any():
                raise ValueError(f"label_table key {key_field!r} must be unique and non-null; "
                                 "set key_field to a stable source ID separate from display rank")
            from geoharness.qgis.layout_table import select_roster

            select_roster(frame.drop(columns=frame.geometry.name).to_dict("records"),
                          table.model_dump())
        planned_frames = bool(arguments.layout and (
            arguments.layout.main_frame is not None or arguments.layout.detail_frame is not None
        ))
        # Native frame visibility is measured later, but explicit coordinates must
        # obey the very same CRS/range contract as the simple-map path now.
        if planned_frames and (arguments.map_extent is not None or arguments.layout.detail_extent):
            from pyproj import CRS

            from geoharness.cartography.preflight import _bounds, _geographic_bounds

            display_crs = CRS.from_user_input(arguments.crs)
            for name, bounds in (("map_extent", arguments.map_extent),
                                 ("detail_extent", arguments.layout.detail_extent.model_dump()
                                  if arguments.layout.detail_extent else None)):
                if bounds is not None:
                    values = list(bounds.values()) if isinstance(bounds, dict) else bounds
                    _geographic_bounds(_bounds(values, name=name), display_crs, name=name)
        report = validate_frames(
            frames, map_crs=arguments.crs,
            map_extent=None if planned_frames else arguments.map_extent,
            detail_extent=(arguments.layout.detail_extent.model_dump()
                           if not planned_frames and arguments.layout
                           and arguments.layout.detail_extent else None),
            coerce_numeric=False, minimum_span_geographic=settings.minimum_span_geographic,
            minimum_span_projected=settings.minimum_span_projected,
            extent_padding=settings.extent_padding,
        )
        if planned_frames:
            # Field/geometry/CRS checks use the whole frozen input. Actual visibility
            # belongs to the final native frame union, after font/content measurement.
            from geoharness.cartography.preflight import named_bounds, to_imshow_extent

            report["visibility_scope"] = "deferred_to_final_native_frame_union"
            if arguments.map_extent is not None:
                report.update(map_extent=arguments.map_extent, map_extent_source="explicit",
                              named_bounds=named_bounds(arguments.map_extent),
                              imshow_extent=to_imshow_extent(arguments.map_extent))
        from geoharness.cartography.composition import composition_risks

        report["leader_checks"] = leader_checks
        report["composition"] = composition_risks(
            frames, report=report, layout=arguments.layout.model_dump() if arguments.layout else {},
        )
        from geoharness.analysis.result_package import infer_packages, require_candidate_bindings
        from geoharness.cartography.facts import infer_analysis_manifests, load_analysis_facts

        roots = qgis_read_roots(config, context.cwd)
        manifests = list(dict.fromkeys([
            *arguments.analysis_manifests,
            *infer_analysis_manifests(mapped_sources, cwd=context.cwd, read_roots=roots),
            *infer_packages(context.metadata, mapped_sources),
        ]))
        report["analysis_evidence_status"] = "verified" if manifests else "not_provided"
        report["analysis_manifests"] = manifests
        if manifests:
            report["analysis_binding_note"] = (
                "Native output references/paths retain analysis provenance automatically; "
                "all supplied and inferred manifests are verified against actual mapped outputs."
            )
        if manifests:

            report["analysis"] = load_analysis_facts(
                manifests, cwd=context.cwd,
                read_roots=qgis_read_roots(config, context.cwd), mapped_sources=mapped_sources,
                display_crs=report["display_crs"],
            )
            require_candidate_bindings(context.metadata, mapped_sources, report['analysis'])
        elif context.metadata.get('geo_task_record', {}).get('analysis_requirement') == 'required':
            raise ValueError(
                '当前任务已产生分析结果，但制图输入尚未绑定结果事实。'
                '先用geo_task publish_analysis发布当前实际选集，'
                '再将该结果作为图层；不要重复渲染无分析来源的工程。')
        return report


def _apply_native_frame_evidence(preflight: dict, details: dict) -> None:
    """Expose final native windows rather than the preliminary data envelope."""
    from geoharness.cartography.preflight import named_bounds, to_imshow_extent

    plan = details["layout_plan"]
    preflight.update(layout_plan=plan, composition=details["composition"],
                     frame_checks=details["frame_checks"])
    constraints = details['frame_checks'].get('symbol_constraints', {})
    preflight['render_feasible'] = constraints.get('feasible', True)
    if not preflight['render_feasible']:
        preflight['status'] = 'needs_repair'
        preflight['issues'] = constraints.get('unresolved', [])
    if details.get("layout"):
        preflight["layout"] = details["layout"]
    extent = plan["frames"]["main_map"]["extent"]
    preflight["preliminary_extent"] = preflight["map_extent"]
    preflight.update(map_extent=extent, named_bounds=named_bounds(extent),
                     imshow_extent=to_imshow_extent(extent),
                     visibility_scope=("declared_frame_subjects_and_legacy_union"
                         if details["frame_checks"].get("subjects", {}).get("checked")
                         else "union_of_rendered_map_frames"))
    for layer, checked in zip(preflight.get("layers", []), details["frame_checks"]["layers"]):
        counts = checked["frames"]
        layer.update(visible_feature_count=checked["visible_feature_count"],
                     frame_visibility=counts, visibility_valid=checked["valid"],
                     visible_main=bool(counts.get("main_map", 0)),
                     visible_detail=bool(counts.get("detail_map", 0)),
                     visible_main_feature_count=counts.get("main_map", 0),
                     visible_detail_feature_count=counts.get("detail_map", 0))
    detail = plan["frames"].get("detail_map")
    preflight.update(detail_extent=detail["extent"] if detail else None,
                     detail_named_bounds=named_bounds(detail["extent"]) if detail else None)
    preflight["warnings"] = [item for item in preflight.get("warnings", [])
                              if not item.endswith("is outside map/detail extent.")]
    preflight["warnings"].extend(details["frame_checks"].get("warnings", []))


async def _drain_task(task: asyncio.Task) -> None:
    """Finish an owned reader before deleting its files, even on repeated cancel."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        task.exception()  # Retrieve failures without replacing the original cancel/timeout.
