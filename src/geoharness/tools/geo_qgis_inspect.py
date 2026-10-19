"""Model-facing read-only project inspection; compilation is owned by delivery."""

from typing import Literal

from openharness.tools.base import BaseTool
from pydantic import Field

from geoharness.cartography.scene import Contract


class QgisInspectInput(Contract):
    action: Literal['inspect'] = Field(default='inspect', description='固定inspect，只读已有工程。')
    project_path: str = Field(min_length=1, description='用户已有或交付返回的真实.qgs/.qgz文件。')
    detail_path: list[str | int] = Field(default_factory=list,
        description='返回对象中的实际JSON路径；省略读取概要。')
    offset: int = Field(default=0, ge=0, description='只读列表起始索引。')
    limit: int = Field(default=10, ge=1, le=100, description='只读窗口项数。')


class GeoQgisProjectReadTool(BaseTool):
    name = 'geo_qgis_project'
    description = ('只读检查用户已有或最终交付的QGIS工程。不能创建/构建/渲染地图。'
        '新图和修图用geo_cartography原生计划，全部图片审查通过后geo_deliver一次生成工程。')
    input_model = QgisInspectInput

    async def execute(self, arguments, context):
        from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

        return await GeoQgisProjectTool().execute(GeoQgisProjectInput.model_validate(
            arguments.model_dump(exclude_unset=True) | {'action': 'inspect'}), context)

    def is_read_only(self, arguments):
        return True
