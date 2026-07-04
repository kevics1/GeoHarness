# DE-9IM 拓扑关系

## 9-交模型

两个几何体A、B，各3个部分（Interior/Boundary/Exterior），组合9种交集：

```
         A.I    A.B    A.E
B.I  [  I∩I   I∩B   I∩E  ]
B.B  [  B∩I   B∩B   B∩E  ]
B.E  [  E∩I   E∩B   E∩E  ]
```

交集结果：0(点) / 1(线) / 2(面) / ∅(空) / T(非空)

## 8种基本拓扑关系

| 关系 | 含义 | DE-9IM模式 | 示例 |
|------|------|-----------|------|
| Equals | 几何相同 | TFFFTFFF² | 同一多边形 |
| Contains | A包含B | T*TFF*FF* | 湖北包含武汉 |
| Within | A在B内 | T*FF*FF** | 武汉在湖北内 |
| Crosses | 线穿越面/线交叉 | T*T****** | 长江穿越武汉 |
| Overlaps | 同维部分重叠 | T*T***T** | 两个缓冲区重叠 |
| Touches | 边界接触 | FT******* | 武汉触摸鄂州 |
| Disjoint | 完全分离 | FF*FF**** | 武汉与北京 |
| Intersects | 非分离(Contains+Within+Crosses+Overlaps+Touches) | T******** | 任意有交集 |

## 常用判断

```
A包含B？ → Contains(A, B)
B在A内？ → Within(B, A)  # 等价于Contains(A, B)
A与B相邻？ → Touches(A, B)
A穿越B？ → Crosses(A, B)  # 线穿面 或 线穿线
A与B有交集？ → Intersects(A, B)  # Disjoint的否定
```

## MCP工具使用

```
geo_spatial_relation(
  geom_a="POLYGON((...))",  # 湖北省
  geom_b="POINT(114.31 30.59)",  # 武汉市中心
  relation="contains"
)
→ true

geo_spatial_relation(
  geom_a="LINESTRING((...))",  # 长江
  geom_b="POLYGON((...))",  # 武汉市
  relation="crosses"
)
→ true
```
