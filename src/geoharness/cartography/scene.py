"""Renderer-independent map contracts. Coordinates on paper are millimetres."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Category(Contract):
    value: str | int | float | None = Field(description='真实分类值；null代表空值。')
    label: str = Field(description='图例显示名称；不能修改分析分类含义。')
    color: str = Field(description='颜色，例如#d73027；支持matplotlib合法颜色。')


class Symbol(Contract):
    color: str = Field(default='#4186b5', description='填充颜色，默认蓝色；none为空心。')
    outline: str = Field(default='#333333', description='边线颜色，默认深灰。')
    width_mm: float = Field(default=.2, ge=0, le=10, description='边线宽度，毫米。')
    size_mm: float = Field(default=1.5, gt=0, le=30, description='点符号直径，毫米。')
    opacity: float = Field(default=1, ge=0, le=1, description='透明度0到1；图例同样应用。')
    field: str = Field(default='', description='分类的真实字段；有categories时必填。')
    categories: list[Category] = Field(default_factory=list, max_length=64,
        description='分类值、名称和颜色；按此顺序显示图例，未声明的值拒绝绘制。')

    @model_validator(mode='after')
    def categories_match_field(self):
        if bool(self.field) != bool(self.categories):
            raise ValueError('分类field与categories必须同时声明；不猜分类或静默画成单色。')
        from matplotlib.colors import to_rgba

        for color in [self.color, self.outline, *(c.color for c in self.categories)]:
            to_rgba(color)
        return self


class Layer(Contract):
    id: str = Field(min_length=1, max_length=80, description='稳定图层ID，编辑/页面引用使用。')
    source_ref: str = Field(min_length=1, description='实际文件路径或已返回的data:引用。')
    name: str = Field(default='', description='显示名称，默认使用id。')
    kind: Literal['vector', 'raster'] = Field(default='vector', description='矢量或栅格。')
    source_layer: str = Field(default='', description='多图层GPKG的真实图层名；不猜测。')
    key: str = Field(default='', description='实体唯一非空字段；选择或标注时必须提供。')
    select_keys: list[str | int | float] | None = Field(default=None,
        description='按key选择实际对象；null保留全部，空数组为空选集，不移动几何。')
    label_field: str = Field(default='', description='标注字段；文字可避让，真实点位不移动。')
    label_size_pt: float = Field(default=8, ge=5, le=30, description='标注字号，pt。')
    label_offsets_mm: dict[str, list[float]] = Field(default_factory=dict,
        description='可选：真实实体key字符串到标注(dx,dy)纸面毫米偏移；只移动文字，自动连接原点。'
        '无需生成标签点/引线文件或重新发布分析。省略的实体自动避让。')
    show_in_legend: bool = Field(default=True,
        description='是否进入专题图例；重复强调层、辅助编号层可设false，不改变实际绘制。')
    symbol: Symbol = Field(default_factory=Symbol, description='统一原生及最终工程符号。')

    @model_validator(mode='after')
    def label_offsets(self):
        import math

        if self.label_offsets_mm and (not self.key or not self.label_field):
            raise ValueError('label_offsets_mm需要key与label_field')
        if any(len(v) != 2 or not all(math.isfinite(n) and abs(n) <= 200 for n in v)
               for v in self.label_offsets_mm.values()):
            raise ValueError('标注偏移须为两个有限毫米数，绝对值不超过200')
        return self


class Table(Contract):
    layer: str = Field(description='真实图层ID。')
    fields: list[str] = Field(min_length=1, max_length=12, description='表格字段及顺序。')
    headers: dict[str, str] = Field(default_factory=dict, description='字段到列名的字典。')
    decimals: dict[str, int] = Field(default_factory=dict,
        description='可选，数值字段到显示小数位0–8；默认最多2位并去尾零。仅格式化名册，不改分析原值。')
    sort_by: str = Field(default='', description='排序字段，默认保留数据顺序。')
    rows_per_page: int = Field(default=24, ge=1, le=60,
        description='每页行数；自动续页保留全部对象，不截断。')


class Coverage(Contract):
    layer: str = Field(description='应覆盖对象的真实图层ID；必须声明唯一key。')
    drawn_layers: list[str] = Field(min_length=1,
        description='本页实际承载这些对象的图层ID；逐实体键与真实几何核对。')
    keys: list[str | int | float] | None = Field(default=None,
        description='应覆盖的实体键；省略为layer的全部实际对象，不能按标题猜。')


class PageLayout(Contract):
    placement: Literal['auto', 'side', 'top', 'bottom', 'manual'] = Field(default='auto',
        description='图例布局策略；auto比较侧栏/上栏/下栏并选最大可用地图面积，不靠改extent试版。')
    map_rect_mm: list[float] | None = Field(default=None, min_length=4, max_length=4,
        description='manual时必填：纸面左、上、宽、高，毫米；系统拟合实际图框并返回真实矩形。')
    legend_rect_mm: list[float] | None = Field(default=None, min_length=4, max_length=4,
        description='manual时可选图例矩形：左、上、宽、高，毫米；不能压盖地图框。')
    margin_mm: float = Field(default=9, ge=3, le=50, description='纸张边距，毫米。')
    gap_mm: float = Field(default=6, ge=2, le=30, description='图框与图例间距，毫米。')
    legend_width_mm: float = Field(default=55, ge=25, le=200, description='侧栏图例宽度，毫米。')
    fit: Literal['extent', 'frame'] = Field(default='extent',
        description='extent保留地理范围并拟合图框；frame保留图框比例，仅向外扩展地理范围，不裁实体。')

    @model_validator(mode='after')
    def rectangles(self):
        if self.placement == 'manual' and not self.map_rect_mm:
            raise ValueError('manual布局必须提供map_rect_mm')
        if self.placement != 'manual' and (self.map_rect_mm or self.legend_rect_mm):
            raise ValueError('明确图框/图例矩形时placement必须为manual')
        for rect in (self.map_rect_mm, self.legend_rect_mm):
            if rect and (min(rect[:2]) < 0 or min(rect[2:]) <= 0):
                raise ValueError('矩形左上坐标不能为负，宽高必须为正')
        return self


class Page(Contract):
    id: str = Field(min_length=1, max_length=80, description='稳定页面ID，改其他页不变。')
    title: str = Field(default='', description='页面标题；支持{subject_count}，需唯一coverage。')
    role: Literal['map', 'detail', 'roster'] = Field(default='map', description='页面验收职责。')
    purpose: str = Field(default='', max_length=500,
        description='当前页固定表达目的及与其他页分工，例如总览只表达最终范围，时序由详图表达；不是修复进度。')
    review_requirements: list[str] = Field(default_factory=list, max_length=16,
        description='固定的当前页可见内容要求；不填写最终复核等进度措辞。改变会使该页审查失效。')
    layout: PageLayout = Field(default_factory=PageLayout,
        description='纸面图框与图例布局；默认自动求解。')
    layers: list[str] = Field(default_factory=list, description='由底到顶的图层ID；不填使用全部。')
    locator_pages: list[str] = Field(default_factory=list, max_length=16,
        description='在本页绘制指定真实Page.id的实际详图范围框；系统与目标取景同步，无需Python生成定位框。')
    extent: list[float] | None = Field(default=None, min_length=4, max_length=4,
        description='显示CRS的xmin,ymin,xmax,ymax；省略按focus_layers拟合。')
    focus_layers: list[str] = Field(default_factory=list,
        description='范围拟合目标；默认当前页图层。')
    table: Table | None = Field(default=None, description='名册定义；有表格时该页只绘表。')
    coverage: list[Coverage] = Field(default_factory=list, max_length=16,
        description='可选逐实体覆盖承诺；系统核对指定对象确在本页范围内且真实绘出，不能用少量样本冒充全集。')
    footer: str = Field(default='', description='来源/限制文字；分页支持{row_start}/{row_end}/'
        '{total_rows}/{page_number}/{page_count}，由系统按当前页展开。')


class Scene(Contract):
    map_id: str = Field(default='', description='实际地图ID，默认由系统分配。')
    title: str = Field(default='', description='地图总标题。')
    crs: str = Field(min_length=1, description='显示CRS；距离/面积分析CRS单独记在结果包。')
    width_mm: float = Field(default=297, ge=150, le=1200, description='纸张宽度，毫米。')
    height_mm: float = Field(default=210, ge=120, le=1200, description='纸张高度，毫米。')
    dpi: int = Field(default=150, ge=36, le=600, description='原生图片分辨率。')
    extra_formats: list[Literal['svg', 'html']] = Field(default_factory=list,
        description='可选附加SVG或交互HTML；PNG及多页PDF始终生成。HTML交互本身未被静态审查认证。')
    font_family: str = Field(default='', description='已安装字体名；空时检测中文字体。')
    layers: list[Layer] = Field(min_length=1, max_length=32, description='真实来源与样式。')
    pages: list[Page] = Field(default_factory=lambda: [Page(id='main')], max_length=32,
        description='稳定页面列表；总览/重点详图/完整名册分工。')
    analysis_refs: list[str] = Field(default_factory=list, max_length=8,
        description='publish_analysis返回的analysis_ref/package_path或原生分析manifest_path；'
        '不是report_path（Markdown）。省略时按真实来源自动关联。')

    @model_validator(mode='after')
    def identities(self):
        from pyproj import CRS

        CRS.from_user_input(self.crs)
        keys = [layer.id for layer in self.layers]
        if len(keys) != len(set(keys)) or len({p.id for p in self.pages}) != len(self.pages):
            raise ValueError('图层与页面ID必须分别唯一')
        if not self.pages:
            raise ValueError('至少需要一个页面')
        for page in self.pages:
            if (set(page.locator_pages) - {p.id for p in self.pages if not p.table}
                    or page.id in page.locator_pages):
                raise ValueError('locator_pages需引用其他实际地图/详图页面ID，不能引用自身或名册')
            if (page.role == 'roster') != bool(page.table):
                raise ValueError('名册页role=roster与table必须同时声明，避免审查职责错配')
            if page.table and page.coverage:
                raise ValueError('coverage用于地图页；名册完整分页由系统核验')
            if page.table and (set(page.table.decimals) - set(page.table.fields) or any(
                    type(n) is not int or not 0 <= n <= 8 for n in page.table.decimals.values())):
                raise ValueError('table.decimals仅接受fields内字段到0–8位小数的映射')
            refs = [*page.layers, *page.focus_layers, *([page.table.layer] if page.table else [])]
            refs += [v for rule in page.coverage for v in [rule.layer, *rule.drawn_layers]]
            if set(refs) - set(keys):
                raise ValueError('页面引用未知图层ID：' + str(set(refs) - set(keys)))
            if page.extent and (page.extent[0] >= page.extent[2]
                                or page.extent[1] >= page.extent[3]):
                raise ValueError('extent必须是正宽高范围')
            if page.extent and page.focus_layers:
                raise ValueError('extent与focus_layers二选一')
        return self


class Edit(Contract):
    target: Literal['layer', 'page', 'scene'] = Field(description='编辑目标类别。')
    id: str = Field(default='', description='图层或页面稳定ID；scene留空。')
    changes: dict = Field(description='仅变化字段；嵌套对象递归合并，数组整体替换。')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def directory(context):
    config = context.metadata['geoharness_config']
    root = Path(config.cartography.outputs_dir).expanduser()
    root = (root if root.is_absolute() else context.cwd / root).resolve()
    return root / 'native-maps'


def save_scene(scene, context):
    document = scene.model_dump(mode='json')
    identity = digest(document)
    folder = directory(context) / 'specifications'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (identity + '.json')).write_text(json.dumps(document, ensure_ascii=False), 'utf-8')
    return 'map-scene:' + identity


def load_scene(reference, context):
    import re

    if not re.fullmatch(r'map-scene:[a-f0-9]{64}', reference):
        raise ValueError('scene_ref必须是工具返回的真实map-scene:引用')
    path = directory(context) / 'specifications' / (reference.split(':')[1] + '.json')
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('无效制图计划文件')
    raw = json.loads(path.read_text('utf-8'))
    if digest(raw) != reference.split(':')[1]:
        raise ValueError('制图计划发生变化，请保存新版本')
    return Scene.model_validate(raw)


def edit_scene(scene, edits):
    def merge(old, changes):
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(old.get(key), dict):
                merge(old[key], value)
            else:
                old[key] = value

    raw = scene.model_dump(mode='json')
    for edit in edits:
        target = raw
        if edit.target != 'scene':
            target = next((v for v in raw[edit.target + 's'] if v['id'] == edit.id), None)
            if target is None:
                raise ValueError('编辑目标不存在：' + edit.id)
        merge(target, edit.changes)
    return Scene.model_validate(raw)
