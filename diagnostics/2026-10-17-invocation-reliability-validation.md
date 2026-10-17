# 2026-10-17 调用可靠性与紧凑错误展示验收

本轮实现基于 `cd35ac4`，功能与最后契约修复提交截至 `a542f16`。
用户授权全部实施六阶段及调用前提醒；未运行真实模型或原始业务整图，未推送。

## 已实现

1. GeoHarness 自有 React/Ink ToolCallDisplay 扩展，失败显示小 `× → NL`，实际非空行计数。
   配对结果、恢复的孤立结果、默认/codex两种风格均折叠。事件、模型结果与失败状态不改。
   启动时临时复制上游小型前端源码并链接既有依赖；退出/启动失败后清理，不改上游包。
   打印模式仅折叠失败，普通结果兼容。会话级异常继续可见。
2. 全量和精简 QGIS 结果共享预览提取契约，精确授权逐页及名录预览，保留64近期任务、
   成功任务归属、1MiB诊断记录大小、图像后缀、job边界和无链接检查。
3. 依赖无关的 `geoharness.invocation` 共享关键注意事项。系统提示/Python描述/QGIS帮助
   提醒fresh process、真实引用、API签名、read(1)二维、mask索引例外、CRS/transform/nodata、
   稳定身份、图框选择关系、只读selectors、冻结创建和失败后定向修复。
   精简帮助保留cautions，完整说明由detail_call获取；动作短形式不移除旧完整接口。
   常见跨字段失败返回精确字段、关联字段及缺失图层。
4. Rasterio封装read_raster/write_raster/raster_backdrop：masked band-first数组、裁剪/重采样
   时保留地理信息、三维写出前检查、NoData可表达及冲突检查。灰度/RGB仅显示拉伸，
   不代替分析重分类。TIFF读回只验证形状/CRS/transform元信息，不证明科学正确性。
   错误分类区分API/数组/栅格/权限，503数字或timeout文件名不授权原样重试。
5. 主体或zoom失败最多原生试测三个候选，不导出、不自动应用、不减数量/覆盖/硬性zoom。
   明确key选择与原focus范围参与取景；源数量不足、缺失/重复key不会产生虚假范围解。
   候选返回实测结果，旧公式建议标unverified_formula；通过的候选编辑调用绑定失败的
   不可变spec_ref与输入摘要。编辑后仍须新preflight。保留失败前自动尺寸适配，避免测量
   一个布局却返回另一套参数。

## 验证证据

| 维度 | 结果 |
|---|---|
| 阶段1 UI | 5项通过，含真实React/Ink渲染、启动失败恢复、Windows junction清理 |
| 阶段2读取 | 5项通过，含真实受guard子进程、越界/链接/失败job/写入/列目录拒绝 |
| 阶段3帮助 | 55项相关检查；停止并行后21项关键帮助回归通过；1200/2400预算关键规则可见 |
| 阶段4执行与栅格 | 71项通过，包含像素/CRS/transform/NoData读回及源写入拒绝 |
| 阶段5原生布局 | 11项通过；候选回放修复后另4项通过，含原生规范→候选编辑→预检→创建 |
| 最后重试分类 | 1项定向通过，数字503与timeout文件名不误判瞬态 |
| 源码整合 | 121项通过，204.17秒，非全量：`.scratch/oct17-fourth-integrated.xml` |
| 安装版定向 | 29项初次通过；1项测试自身隐式GBK读取UTF-8失败，显式编码修正后补测通过 |
| 安装版实际串联 | 三页、隐藏稳定名录key、栅格helpers、逐页Python读取、facts/claims及实际交付通过 |
| Ruff与diff | 全src及新增测试Ruff通过；无新增diff空白问题 |
| CLI | 源码/正常安装环境version、dry-run通过，16工具；MCP配置但未连接 |

安装版串联记录：`.scratch/oct17-fourth-installed-acceptance.json`。
三页成果仍为 `quality_status=draft`、`visual_acceptance=unverified`；API调用为0。
渲染测试检查了实际终端组件输出，未操作用户桌面窗口。

## 构建、安装与保留项

- 使用既有标准库、Rasterio/Numpy和React/Ink，不新增运行依赖。
- 已检索 [Ink官方实现](https://github.com/vadimdemedes/ink) 与测试工具，当前安装版本的
  render(stdout)已足够，未增加ink-testing-library。
- 栅格依据 [Rasterio官方resampling](https://github.com/rasterio/rasterio/blob/main/docs/topics/resampling.rst)
  和 [mask实现](https://github.com/rasterio/rasterio/blob/main/rasterio/mask.py)，不自研算法。
- 最初pip关闭构建隔离时缺少hatchling，改用项目既有uv隔离构建成功，未往运行环境添加构建包。
- wheel：`.scratch/oct17-fourth-wheel/geoharness-0.1.0-py3-none-any.whl`。
- SHA256：`230f9a8c016c46060ad13a13afa13dbb1953de8b1c6048b003bfd061c2a4ea8d`。
- 已 `--no-deps --force-reinstall` 到 `D:/Python312/Lib/site-packages/geoharness`。
- 101 Python模块、1前端TSX、34随包文件，源码/wheel/安装版零差异；
  `.scratch/oct17-fourth-parity.json`。
- `.env.example`保留SHA256：`b2665ac001462f3915270c2f11259bafe3e8ed30a9e882a163b3edaa69c6fbcd`。
  原始数据、旧图、AGENTS/Skills、上游依赖文件和既有未跟踪诊断未改。

## 实际限制与过程问题

- 并行执行者未遵守分工、返回确认语且发生越界写入，停止并行后主Agent串行完成。
  Python输入模型由已验证基线恢复，重应用本轮补丁；以最终整合验证为准。
- 请求独立审查两次未取得实际审查报告；不能声称独立审查通过。
  主Agent自检发现并修复候选自动尺寸回放与瞬态误分类问题，均有回归。
- `python -I -m geoharness.cli`屏蔽用户site后，既有Typer缺少annotated_doc依赖；
  没有修改上游或依赖环境。正常安装环境的CLI已验证，隔离Python用于工具链和组件验证。
- 未做真实模型完成率比较、真实业务数字审核或最终地图逐页像素验收。
  文件可读、合成链通过、PNG存在均不等于业务/视觉质量通过。
