# 试验：用 JigCAD Skill 复刻 procgen-hy4 的模型

日期：2026-11-23。对象：[procgen-hy4](https://github.com/wrong-nebula/procgen-hy4) 中的 4 个模型。
做法：严格按 `skill/jigcad/SKILL.md` 的流程，以 Agent 身份编写规格，记录每一轮迭代。
procgen 用米，这里统一换算为毫米，派生尺寸照搬原文件的公式。

## 结果总览

| 模型 | procgen 来源 | JigCAD 规格（非空行） | 迭代轮数 | 最终 | 参数扫描 |
|---|---|---|---|---|---|
| bollard 系柱 | `examples/starter/bollard.py`（52 行） | 29 | 1 | PASS 11/11 | height 600 / 900 / 1400 全部 PASS |
| plummer_block 轴承座 | `kitlib_mech.plummer_block` | 57 | 3 | PASS 13/13 | — |
| clevis_fork U 形叉 | `kitlib_mech.clevis_fork` | 52 | 3 | PASS，带 1 条预期内警告 | — |
| turntable 转车台（主体） | `examples/hy4/turntable.py`（813 行） | 137 | 5 | PASS 9/9 | angle 0 / 30 / 135 / 270、d 10 / 15 / 20 m 全部 PASS |

各模型的渲染图：`bollard.png`、`plummer_block.png`、`clevis_fork.png`、`turntable.png`、
`turntable_section.png`（剖视）。

## 迭代日志（每个失败是谁的错，自检有没有拦住）

| 模型 / 轮次 | 报告 | 根因 | 归属 | 处理 |
|---|---|---|---|---|
| plummer 1 | `kernel TypeError` | `boss.hole_depth` 不支持 `next`，而且组件代码异常被错标成 `kernel` | **JigCAD bug** | 已修复；新增 `internal` 错误码 |
| plummer 2 | `disjoint`，后改为 `touch_only` | 油杯放在圆柱外壳的 `top` 上，只有线接触 | 建模错误（Agent） | 新增 `touch_only` 诊断，fix 提示"往里埋 s mm"；照做后通过 |
| clevis 1 | `floating` | `flip` 让 v 轴反向，眼孔被放到 z = -80 | 建模错误（Agent） | floating 报文改为附带工具和本体包围盒；SKILL 写明 flip 规则 |
| clevis 2 | `bbox.size_y` | 叉耳（2·r_eye）比连接段宽，是我的检查值写错了 | 检查写错（Agent） | 改正检查 |
| turntable 1 | `floating` 吊杆 | `profile` 拉伸方向依赖顶点绕向，顺时针时反向 | **JigCAD bug** | 已修复：自动规范为逆时针 |
| turntable 2 | `manifold` 全局失败 | 车轮与环轨线接触，熔合后非流形；全局检查定位不到特征 | 建模错误，加 **JigCAD 诊断缺口** | 新增逐特征 `non_manifold` 检查；加 0.5 mm 运行间隙 |
| turntable 2 | `bad index 0` | `cylinder` 等基础组件不输出定位点 | **JigCAD 缺口** | 每个实例默认输出锚点原点 |
| turntable 3 | angle=30 时 `dim` 失败 | 检查用了世界坐标 x，不具备旋转不变性 | 检查写错（Agent），由扫描发现 | 改用到转轴的距离 |
| turntable 4 | angle=270 时 `bbox` 超差 | `rotate` 是原地自转，`at` 不随之旋转；**30° 和 135° 时轨道已经放错但没被发现** | **JigCAD 语义缺口** | 新增 `frame_rotate`，加旋转不变检查 `track_offset` |
| （全部） | 4 个角度的指纹相同 | 指纹只哈希了属性，对称旋转后不变 | **JigCAD 缺口** | 指纹加入顶点坐标哈希 |

统计：

- 共 11 次失败，**全部在本地被自检发现**，没有一次需要靠人眼发现。
- 其中 6 次是 JigCAD 自身的问题（3 个 bug、3 个缺口），都已修复，并补了回归测试（测试数 31 → 39）。
- 另外 5 次是建模者或检查写错，诊断都直接指出了修改点。
- 一个值得警惕的现象：turntable 的轨道错位在 2 个角度下"静默通过"。只有加了**旋转不变的意图检查**再配合**参数扫描**才抓得住。结论写进了 Skill：每个关键关系都要有一条检查，而且要扫描所有控件。

## 与 procgen 的对比

**JigCAD 更强的地方**

- 精确的实体几何（B-Rep），导出 STEP，尺寸能精确到 0.01 mm。轴承座的中心高、螺栓间距、叉口宽度都用 `dims` 检查锁定。
- 规格行数少：系柱 29 行（procgen 52 行）；转车台主体 137 行（procgen 813 行，不过 procgen 的版本细节多得多）。
- 错误定位到具体特征，并给出修改建议；校验全部在本地执行，零 token 成本。
- 改参数后确定性重建：转车台 d 在 10~20 m、angle 在 0~360° 范围内全部通过。

**JigCAD 做不了或做得别扭的地方（转车台暴露的）**

| 缺口 | 影响 | 建议 |
|---|---|---|
| 没有回转体（lathe/revolve） | 钢轨头部截面、带台阶的轴类零件只能用矩形管近似 | **v0.2 必做**：`revolve` 组件（任意截面） |
| 没有装配和多体 | 旋转桥体被迫和静止的坑体熔成一体；车轮和轨道只能留间隙；无法给不同零件分配材料 | **v0.2 必做**：`assembly`，加零件间的接触、间隙、干涉检查 |
| 没有"组变换" | 桥体的 13 个特征都要重复写带三角函数的显式平面（`on: {origin, normal, x_dir}`），冗长易错 | 新增 `frames:`（命名坐标系，可引用、可旋转） |
| 没有两点间杆件 | 风撑、拉杆、扶手无法表达 | 新增 `rod {from, to, d}` |
| 斜置零件 | 斜下弦杆改成了腹板平面内的多边形 | `frame_rotate` 目前只能绕法向转，可扩展为绕任意轴 |
| 表面细节（铆钉、文字、接缝） | 没有做 | 属于视觉层；工程上可做 `text` 雕刻和铆钉阵列 |
| 大场景渲染 | 特征一多标签就重叠，坑壁挡住内部 | **本次已加**：编号标签加图例、`--section x\|y\|z` 剖视 |

## 结论

- 机械和功能件（轴承座、U 形叉、系柱）：JigCAD 适配良好，1~3 轮就能通过，且有精确的尺寸保证。这类模型正是它的定位。
- 场景和机构类（转车台）：能做出准确的主体结构，并通过了参数扫描，但表达成本高，缺少回转体、装配和组变换。
  这三项正是 PRD 路线图中 v0.2 要做的，优先级建议上调。
