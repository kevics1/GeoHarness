# 有放大图和重点标签的 Python 布局

复用现有分析结果。先绘图层、设置最终坐标范围和比例，再放标签；不要在标签放置后改动轴的位置或调用 tight_layout。图例和说明各占独立区域，不放在地图内部。

```python
from geoharness.cartography.layout import (
    create_map_figure, place_point_labels, save_map_figure,
)
from geoharness.config.settings import MapLayout

layout = MapLayout(width=16, height=11, dpi=180, title_font_size=20)
page = create_map_figure(layout, title="分析结果", subtitle="数据、口径与时间", detail=True)
# main_layers 和 focus_layers 来自实际分析图层；使用完全相同的分级及颜色。
for axis, extent in [(page.main, main_extent), (page.detail, focus_extent)]:
    # 在这里绘制已有 GeoDataFrame/栅格，并显式锁定 extent。
    axis.set_xlim(extent[0], extent[2])
    axis.set_ylim(extent[1], extent[3])
    axis.set_aspect("equal")

# priority_points 按重要性排序，元组为 (x, y, 名称或编号)。
placements = place_point_labels(
    page.detail, priority_points, font_size=9, max_offset_points=108,
    # 可用所有可见符号的 (x, y, 半径点数) 避免标签遮住符号。
    protected_points=visible_symbols,
)
# 放不下的重点名字必须在名单中提供；可全部改用编号并列出对应姓名。
missing = [p for p in placements if not p["placed"]]
page.legend.legend(handles=legend_handles, loc="upper left", ncol=4, frameon=False)
page.footer.text(0, .95, source_crs_and_method, va="top")
check = save_map_figure(page, RUN_DIR / "map.png")
assert check["status"] == "passed", check
```

输出的 map.layout.json 与实际图片 SHA256 绑定，记录分区相交、标签相交、标签裁切、图例/说明越界和省略标签；它只检查这些布局几何项，不证明地图数据、分级含义或图层可见性正确。geo_map_review 会读取匹配的报告，先要求修复明确布局问题，再调用视觉服务。不能以颜色计数、像素比例或这个布局报告代替视觉审查。

生成工程时传同一 PNG 的已存在路径和真实来源；分级图层显式指定 column/field_name、categories、colors。调用 geo_deliver 时 primary_map 指向该 PNG，review_path 指向该版本的检查报告。工程预览与主图不一致时会拒绝交付；视觉状态 unverified/issues 时按草稿交付。
