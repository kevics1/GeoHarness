# 最新测试紧急修复

用户明确要求最快修改，可只检查是否能运行，实际业务测试由用户执行。
本轮不跑完整pytest，不把运行检查等同于业务通过。

已修改：
- 工程导出由跨provider字符串哈希改为完整几何/属性多重集比较。仅float/float允许4 ULP
  尾差；整数和跨整数数值不放宽，文本与几何继续严格核验。快照重新打开仍用精确哈希。
  实际差异返回layer_id/feature_id/geometry_match/attribute_differences。
- 原生制图日期比较统一Timestamp、明确日期字符串与JSON日期分类值。冻结时保留原字符串
  或日期类型对应值，最终QGIS日期分类转换为QDateTime/QDate，不改写业务源字段。
- render失败响应返回本次已保存scene_ref、map_id、inspect_call和修正基准提示。
- Python结果自动转换已知NumPy/日期/Path/NA/NaN/NaT类型；DataFrame/GIS对象仍须文件化。
  超长结果另存console-result.json并返回文件引用，完整内容不截断；已有同名文件不覆盖。
- 已登记read_vector/resolve_data/read_raster动态循环在无显式绑定时保守绑定完整现有目录，
  运行时仍核对实际读取。直接动态文件路径仍须input_refs。明确已有文件在授权根内自动绑定。
- 提示词/帮助纠正当前工具目录标题、失败场景基准、scope_name位置与诊断入口。
  旧技能中的历史制图说明明确服从当前Schema，不修改技能指令文件。

运行检查：
- 真实QGIS只读比较原失败行政区快照，新比较器接受513401.0与513400.99999999994；
  同时拒绝513402.0及不同文本值。该检查完成后原测试产物已被外部清空，未完整重跑旧交付。
- Timestamp/斜杠时间/ISO分类比较运行检查通过。
- 真实隔离Python进程完成NaN/NA/NaT转换和4931字符结果自动文件化，执行状态succeeded。
- 明确文件和登记目录动态助手绑定检查通过；QGIS字段与QDateTime API运行检查通过。
- 安装后模拟render失败确实返回新scene_ref及inspect_call；首次检查脚本漏写必填crs，
  补齐后运行通过，该问题不属于产品失败。
- Ruff与compileall通过，实际安装后dry-run注册17工具。
  136模块+34技能资源源码/wheel/实际安装零差异；wheel SHA256为
  63425c16f90db7ab856850026916a46af8c46c59fabbad84dfd1e689b1f2cd3e。
  用户配置SHA256仍为9ef6ec325f390d529524a0874ac6ab62856cd8a9982a30d0580490f06ca51c5a。

未修改用户配置、high思考强度、审图模型或3/12上限。未重跑真实视觉服务、未变更旧任务
视觉状态，也没有把历史失败报告改写为通过。完整业务链及错误率由用户本轮实测确认。

## 登记状态紧急补丁 P

用户报告OperationOutcome拒绝retained_previous_evidence使整轮异常。根因是performance在工具执行后记录观察时，将生产者的证据状态直接传给只接受committed/failed/not_applicable的任务模型。现于边界统一映射：reused→committed（已登记复用）、retained_previous_evidence/unregistered→not_applicable（没有本次提交）、未知状态→failed；不更改像素结论，unverified仍保留。9项task_record定向检查通过（8.37秒），不跑完整回归。已安装，dry-run通过；136模块+34资源源码/wheel/实际安装零差异，用户配置SHA不变。wheel SHA256为7bbf2f5b63af5125a62e5d0f1637f5b895db021261b8c976240d7289ad82ee98。
