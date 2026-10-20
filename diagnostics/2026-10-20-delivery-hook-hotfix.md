# 森林火灾改3交付阻塞修复

- 已复现：真实GeoHookExecutor将最终工程内部`native_scene_export/plan_path/candidate`
  按公开geo_qgis_project输入校验，产生与用户完全一致的三字段错误。旧工程测试未接真实钩子。
- 已修复：内部编译事件归属geo_deliver，tool_input使用真实GeoDeliverInput；内部动作及候选
  标识单独存为事件元信息。保留PRE/POST与拒绝行为，不关闭参数校验或权限保护。
- 内部编译失败分类为project_build_failed/internal_project_export，提示修复工具/环境，
  不再误导轮换project_dir/map_id/spec_ref。确定失败写入failed日志，不冒充未知运行中。
- 恢复真实业务后发现第二个缺陷：自动详图定位框图例的边线颜色未进入冻结调色板，
  导致KeyError '#222222'。新计划收集页面图例颜色；旧计划按其显式六位HEX精确转换RGBA，
  不猜颜色、不改冻结文件或已审图片。
- 旧失败尝试经精确request/result记录核对：动作、计划路径及SHA一致，终态failed，
  无qgz生成。保留原日志到scene-materialization.failed-native_scene_export-20261020T015531Z-f9245f9f3244.json
  后，在实现改变的条件下恢复一次；不清除未知状态或盲目重试。

## 验证

- 真实钩子回归在修改前精确失败；修复后含矢量/栅格、定位框图例、钩子拒绝及工程一次生成
  的11项定向回归通过，45.29秒。没有重新运行全量测试。
- 真实森林火灾改3任务83945dd6-b590-42e8-889b-c39f4076912a，地图
  7ff9f2a9-bab0-4a31-902d-3a624000119f，选定版本2保持不变。恢复原始交付请求成功，
  49个文件，PDF11页，全部11张已审PNG的SHA与交付副本一致。没有重新分析或请求视觉服务。
- 成果：F:/Desktop/Project/GeoHarness/example/森林火灾改3/exports/西昌市330森林火灾威胁评估-t99y1zs7。
  可编辑工程project/map.qgz；编译器实际重新打开工程并核对图层、页数、布局、文本等冻结属性。
- 安装包已更新到D:/Python312/Lib/site-packages，136模块+34技能资源与源码/wheel零差异。
  配置SHA保持9ef6ec325f390d529524a0874ac6ab62856cd8a9982a30d0580490f06ca51c5a。

本次修复交付阻塞，不改写人口缺失、道路直线距离口径或预测时次含义；工程/交付检查通过
不等于对分析科学正确性重新验收。
