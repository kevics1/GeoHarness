"""共享的紧凑注意事项，可在工具包初始化前使用。"""

from __future__ import annotations

PYTHON_CAUTIONS = (
    "每次调用都是新进程和新 RUN_DIR：必须重新 import/定义所有名称；前序文件通过 "
    "input_refs/INPUTS 或返回的准确路径绑定。",
    "使用中间结果前核对实际列名、CRS 和库函数签名；input_requirements 不会对任意代码"
    "做类型检查。",
    "`rasterio read(1) is 2D`（二维结果）：不要盲目追加 [0]。mask_raster 返回 (array, transform)，"
    "通常为 band-first（indexes=int 时得到二维）；叠加或写出前先检查 shape。"
    "需要始终为三维数组时优先使用 read_raster/write_raster。",
    "让已验证的 CRS/transform/nodata 跟数组一起传递；rasterio.mask.mask 使用 "
    "`nodata, not fill_value`（nodata 而非 fill_value）。只能写 RUN_DIR；保留源数据。",
    "失败后先检查实测原因，修改对应输入/代码，只重跑失败阶段；未变化的重复调用仅用于"
    "已确认的瞬态故障。",
)

NATIVE_CARTOGRAPHY_CAUTIONS = (
    "geo_cartography 只做原生渲染，不启动 QGIS、不生成 .qgs/.qgz；普通制图、修图和"
    "审图都走这里。不要调用 geo_qgis_project 构建普通地图。",
    "纸面坐标单位是毫米(mm)，字体单位是 pt；extent 使用 Scene.crs 的坐标单位。"
    "Layer.key 是真实唯一非空实体字段，select_keys 只选择对象，不移动真实几何。",
    "页面从底到顶声明 layer id；extent 与 focus_layers 二选一。table.layer 必须是"
    "实际 Layer.id；分类图例 categories 必须覆盖实际字段值，不能写占位符。",
    "首次传完整 scene；之后用返回的 scene_ref + edits 按稳定 id 合并修改，不要重复"
    "整套 scene。渲染返回 layout.problems 与 page_previews，先修确定缺陷再审图。",
    "geo_map_review(all_pages=true) 只读取实际页面像素并登记审查；不改图。"
    "全部必要页面通过且没有待处置问题后，geo_deliver 才生成一次可编辑工程。",
    "Page.layout自动编译图框/图例；label_offsets_mm与locator_pages处理文字/引线/范围框，"
    "不需要Python测图或复制辅助业务数据。验收职责写purpose/review_requirements，修复进度写repair_notes。",
)

QGIS_CAUTIONS = (
    "首选 build(spec_ref, edits)：一个原生作业完成验证、渲染和打包。首次草案用 "
    "action=spec 并给 layers；已有草案才用 spec_ref。旧版冻结 create 只接受 "
    "action+plan_ref；任何变更都需要重新构建。",
    "每个图框二选一：FIT focus_layers 或显式 extent，绝不能同时使用。"
    "render_layers 必须包含 focus_layers/label_layers；详图必须有范围来源。"
    "table_only 图层只能做名单源，不能出现在任何图框的 render/focus/label 列表。",
    "保留稳定的 layer_key 和声明的对象覆盖要求。收紧范围必须保持覆盖；实际缩放为原生"
    "拟合后的 main_scale/detail_scale。使用返回的 candidate_edit_calls 及其准确的 "
    "base_spec_ref，然后重新 build；不要猜索引或显示名称。",
    "诊断用的 detail_path/offset/limit 只是 help/inspect 的只读选择器；不要把它们复制"
    "到 build/spec/create/preflight。",
    "名录 key_field（或第一个字段）必须唯一且非空；top_n 需要 sort_by 且会排除 keys。"
    "测量表格/图例空间；build/create 只证明工程产出，不代表视觉验收。",
    "普通制图/修图/审图不生成工程，也不以本工具作为默认制图入口。只有用户明确检查/修改"
    "已有工程，或已通过审图的冻结候选需要最终工程时才使用。最终工程由geo_deliver一次生成；"
    "不能只交PNG/PDF，也不能把截图当可编辑工程。",
)

def invocation_guide(tool_name: str) -> str:
    rules = (PYTHON_CAUTIONS if tool_name == "geo_python" else
             NATIVE_CARTOGRAPHY_CAUTIONS if tool_name == "geo_cartography" else
             ("geo_qgis_project仅inspect读取用户已有或交付后的工程；project_path为真实.qgs/.qgz路径。",
              "没有build/create/export动作。制图使用geo_cartography；审图通过后geo_deliver一次生成工程。"))
    return "调用前注意事项（CALL-TIME CAUTIONS）：" + " ".join(rules)

def section_cautions(section: str) -> list[str]:
    by_section = {
        "frame": [QGIS_CAUTIONS[1], QGIS_CAUTIONS[2]],
        "layout": [QGIS_CAUTIONS[1], QGIS_CAUTIONS[2], QGIS_CAUTIONS[4]],
        "roster": [QGIS_CAUTIONS[4]],
        "layer": ["图层按从下到上绘制，先写底层、后写顶层；table_only 永不渲染。"
                  "把 renderer/field/categories 放在 style 内，并使用实际源字段。"],
        "style": ["使用 style.renderer/field/categories/ranges；保留实际分类值。"
                  "QGIS 的 mm/pt 尺寸与 Matplotlib 的点面积不是同一单位。"],
        "labels": ["检查实际几何/标注字段。label_layers 必须被渲染；purpose 是元数据，"
                   "detail_title 才是可见文本。"],
        "edits": [QGIS_CAUTIONS[0], QGIS_CAUTIONS[2]],
        "workflow": list(QGIS_CAUTIONS),
    }
    return list(by_section.get(section, QGIS_CAUTIONS))

def action_forms() -> dict:
    """推荐短调用字段；旧版完整输入仍兼容。"""
    return {
        "help": ["action", "section?", "detail_path?", "offset?", "limit?"],
        "spec:new": ["action", "layers", "title?", "crs?", "layout?", "dpi?"],
        "spec:edit": ["action", "spec_ref", "edits or patches"],
        "build": ["action", "spec_ref", "edits or patches?"],
        "preflight": ["action", "spec_ref"],
        "create": ["action", "plan_ref"],
        "inspect": ["action", "spec_ref or plan_ref or project_path or map_id"],
        "export": ["action", "project_path", "layout_name?", "output_format?"],
        "recover": ["action", "map_id"],
    }
