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
    symbol: Symbol = Field(default_factory=Symbol, description='统一原生及最终工程符号。')


class Table(Contract):
    layer: str = Field(description='真实图层ID。')
    fields: list[str] = Field(min_length=1, max_length=12, description='表格字段及顺序。')
    headers: dict[str, str] = Field(default_factory=dict, description='字段到列名的字典。')
    decimals: dict[str, int] = Field(default_factory=dict,
        description='可选，数值字段到显示小数位0–8；默认最多2位并去尾零。仅格式化名册，不改分析原值。')
    sort_by: str = Field(default='', description='排序字段，默认保留数据顺序。')
    rows_per_page: int = Field(default=24, ge=1, le=60,
        description='每页行数；自动续页保留全部对象，不截断。')


class Page(Contract):
    id: str = Field(min_length=1, max_length=80, description='稳定页面ID，改其他页不变。')
    title: str = Field(default='', description='页面标题，默认使用制图总标题。')
    role: Literal['map', 'detail', 'roster'] = Field(default='map', description='页面验收职责。')
    layers: list[str] = Field(default_factory=list, description='由底到顶的图层ID；不填使用全部。')
    extent: list[float] | None = Field(default=None, min_length=4, max_length=4,
        description='显示CRS的xmin,ymin,xmax,ymax；省略按focus_layers拟合。')
    focus_layers: list[str] = Field(default_factory=list,
        description='范围拟合目标；默认当前页图层。')
    table: Table | None = Field(default=None, description='名册定义；有表格时该页只绘表。')
    footer: str = Field(default='', description='已确认的来源/限制文字，不代替事实包。')


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
            if page.table and (set(page.table.decimals) - set(page.table.fields) or any(
                    type(n) is not int or not 0 <= n <= 8 for n in page.table.decimals.values())):
                raise ValueError('table.decimals仅接受fields内字段到0–8位小数的映射')
            refs = [*page.layers, *page.focus_layers, *([page.table.layer] if page.table else [])]
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
