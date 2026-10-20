"""共享的紧凑注意事项，可在工具包初始化前使用。"""

from __future__ import annotations

PYTHON_CAUTIONS = (
    "最新实测：drop(columns='geometry')后不能再取该表['geometry']；先保存GeoSeries及其index/CRS，"
    "拼接属性后用同索引GeoSeries重建GeoDataFrame。GeoSeries(gdf)或list(gdf)得到列名，"
    "须用gdf.geometry/list(gdf.geometry)，不能把DataFrame当几何序列。",
    "每次调用都重新声明input_refs别名；fire_cur/res等自定义别名不是全局持久名称。"
    "data:ID和operation_id逐字复制工具返回完整值，不编造、不截成12位；"
    "取证用返回的evidence_call/next_call，不猜ID。",
    "脚本自己的json.dumps/写JSON发生在返回前，不能依赖CONSOLE_RESULT事后转换；"
    "用json.dumps(json_ready(value),ensure_ascii=False,allow_nan=False)。"
    "to_markdown依赖tabulate，不能假定已安装；用to_csv或简单Markdown拼接，不为报告安装包。",
    "发布事实先检查将要导出的实际文件字段：在投影表算出的area_km2/cum_area_km2不会自动"
    "出现在原表；按sid等稳定键把结果写入实际输出，再publish_analysis声明字段。",
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
    "近期实测易错项：Path须先from pathlib import Path；循环变量i等须在当前脚本定义。"
    "先describe_input检查中间对象的字段/类型；合并后字段可能带后缀，不能猜label/gid或汇总JSON键。"
    "Timestamp是标量，不能切片；格式化用isoformat/strftime。",
    "CONSOLE_RESULT只放有界JSON：NumPy标量用.item()，数组用.tolist()，日期用.isoformat()，"
    "Path用str()，NA/NaN等缺失值转None而非0；GeoDataFrame/geometry另存文件并返回真实路径，"
    "不能原样塞进结果。",
    "已登记read_vector/read_raster动态循环可自动绑定；其他动态文件读取须用input_refs声明"
    "具体文件；精确字面文件路径在授权范围内自动绑定。RUN_DIR是目录，manifest_path是文件，"
    "不得把二者当业务输入文件/目录互换。data:引用逐字复制实际返回值，不编造哈希；"
    "Windows字面路径用正斜杠或原始字符串，避免反斜杠r/t/n变控制字符。",
    "距离/面积原值保留完整精度，不在分析字段round取整，显示格式交给制图；缓冲/叠置后先查"
    "is_valid，出现自交须核实并修复几何再写输出，保留来源，不用忽略错误代替修复。"
    "同任务geo_python/发布/制图写操作串行；锁冲突先读状态，不新建任务或重复执行分析。",
    "Python后端会转换常见NumPy/日期/Path/缺失值；超长CONSOLE_RESULT自动保存为"
    "console-result.json并返回offloaded/file_path，计算仍成功，不要重做。"
    "排错读取台账用geo_task(action=evidence,operation_id=...,section=result)，"
    "不要Python读取tasks/evidence；scene-plan.json是文件而非目录。"
    "publish_analysis的scope_name若需填写放在facts内，不能猜顶层字段，以当前Schema为准。",
    "最新实测：动态循环读取前把全部文件逐项放input_refs；从时间索引生成t0/t1等列后，"
    "在筛列/merge后的实际对象检查columns，不能假定这些列仍存在。数值索引可能因赋值变成"
    "float，f't{0.0}'得到t0.0而非t0；确认整数语义后统一int再生成/读取列名。"
    "JSON序列化前执行json.dumps(CONSOLE_RESULT,allow_nan=False)自检；浮点列直接where(None)"
    "可能仍保留NaN，表格先astype(object).where(pd.notna(df),None)再转记录；"
    "嵌套数组/标量中的NaN、Infinity也须转None，不以0填补未知。",
)

NATIVE_CARTOGRAPHY_CAUTIONS = (
    "当前默认single_pass：初次render后立即全册审图一次，最多合并修改render一次，然后"
    "直接geo_deliver交付图片/PDF和QGIS工程；不复审、不反复修布局。局部标签碰撞作为交付备注，"
    "不要用Python手算22个标签纸面坐标、反复聚类拆页或随机试偏移。修改后未复审如实注明。",
    "geo_cartography 只做原生渲染，不启动 QGIS、不生成 .qgs/.qgz；普通制图、修图和"
    "审图都走这里。不要调用 geo_qgis_project 构建普通地图。",
    "纸面坐标单位是毫米(mm)，字体单位是 pt；extent 使用 Scene.crs 的坐标单位。"
    "Layer.key 是真实唯一非空实体字段，select_keys 只选择对象，不移动真实几何。",
    "页面从底到顶声明 layer id；extent 与 focus_layers 二选一。table.layer 必须是"
    "实际 Layer.id；分类图例 categories 必须覆盖实际字段值，不能写占位符。",
    "首次传完整 scene；之后用返回的 scene_ref + edits 按稳定 id 合并修改，不要重复"
    "整套 scene。渲染返回 layout.problems 与 page_previews。"
    "single_pass直接审图一轮后合并处理严重问题；strict才先修确定缺陷再审图。",
    "geo_map_review(all_pages=true)只读图片并登记审查，不改图。默认single_pass只做一轮，"
    "最多修图一轮后直接交付；strict模式才要求全部页面通过并处置问题后生成工程。",
    "Page.layout自动编译图框/图例；label_offsets_mm与locator_pages处理文字/引线/范围框，"
    "不需要Python测图或复制辅助业务数据。验收职责写purpose/review_requirements，修复进度写repair_notes。",
    "首次render前一次核对所有Layer的实际字段/key/分类值/选集与事实绑定：子集不继承假想字段；"
    "没有分类字段就用单色。select_keys来自该源真实key值，保留其类型；label_offsets_mm的键是"
    "真实实体key字符串，不能填图层ID、行号或显示排名。",
    "已发布的分析原生产物须直接作为source_ref并出现在地图页layers中，不能只列analysis_refs"
    "或仅放名册。重点子集复用原生产物+select_keys，不另造res_key/标签点文件。",
    "真正新增的派生矢量先按唯一文件publish_analysis并把实际analysis_ref放入Scene.analysis_refs；"
    "别在render报错后逐个补包。原始事件/行政区等背景直接用原始已登记来源，不能为样式复制"
    "为GeoJSON后冒充可证明成员来源的分析产物，也不为过关编造field_changes。",
    "显式extent时focus_layers必须省略或[]；自动聚焦时extent省略。名册table页不能带coverage。"
    "标题只用Schema允许的{subject_count}且须有唯一coverage；footer仅用{row_start}/"
    "{row_end}/{total_rows}/{page_number}/{page_count}，不把业务字段写成模板占位符。",
    "查已有计划用inspect(scene_ref)，不要用未绑定的Python读取scene-plan.json或glob审图报告。"
    "analysis_refs只用实际analysis_ref/package_path或原生manifest_path，不用Markdown报告路径。",
    "Scene.analysis_refs只列本候选确实使用的结果包，每个包的原生产物必须实际入图；"
    "仅作报告/名册的其他产物不要无差别加入。若为新增编号复制key_numbered等矢量，"
    "它不会自动继承原文件的事实绑定；优先直接使用已发布源和已有主键标注，确需派生时"
    "重新发布该文件并核实来源关系，不把旧包强绑新文件。",
    "strict模式审图达到上限是停止通知，不是待回答问题；停止自动改图/重审，整理现有草稿与严重问题，"
    "明确哪些页未完成，不反复要求用户回答通知或用继续重置次数。格式校验失败不同于网络超时。",
    "render失败也会返回包含本次edits的新scene_ref；下一次从该引用修正，不能退回旧引用"
    "撤销前次来源/字段修改。日期对象与日期字符串由工具统一比较，原始字段不改写。"
    "工程导出失败查看返回的layer_id/attribute_differences，不靠重做子集或审图修内部编译器。",
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
              "没有build/create/export动作。制图使用geo_cartography；完成当前审图策略后geo_deliver一次生成工程。"))
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
