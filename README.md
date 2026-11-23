# JigCAD

面向 AI Agent 的低成本、高精度参数化 CAD 工具链：**CLI + Skill**。

Agent 写一份声明式 YAML 规格（参数 + 组件特征 + 验收检查）。`jig` 在本地的 OpenCascade
B-Rep 内核上确定性地构建零件，**逐特征自检**，然后导出 STEP / STL / 3MF / GLB。全部离线运行，不产生任何云费用。
支持**多色 / 多材质**：特征级 `material:` 和 `op: paint`；导出带颜色的 STEP、3MF、GLB，并为每种材质单独导出 STL。

```
$ jig check examples/enclosure.yaml
FAIL enclosure  features 5/5  1.04s  fp=7c39e8573f
stopped at feature 'vents' (earlier features OK)
E breakout [vents[0]] cut breaks through a side face near [15.95, 30.0, 16.5]
  fix: move it inward / reduce size, or set breakout:true / use notch if intended
```

## 安装

```bash
pip install -e .            # 依赖 build123d / pyyaml / numpy / pillow
jig --version
jig skill install --to .claude/skills   # 把 Skill 装进 Agent（任意支持 SKILL.md 的 Agent 均可）
```

## 一个例子

```yaml
part: mount_plate
process: fdm
material: petg
params: {L: 120, W: 60, T: 8, margin: 10}
features:
  - id: base
    use: plate
    with: {l: =L, w: =W, t: =T, r: 5}
  - id: mount_holes
    use: hole
    on: base.top                       # 语义锚点，而不是绝对坐标
    at: ["=L/2 - margin", "=W/2 - margin"]
    repeat: {mirror: xy}
    with: {size: M5, type: cbore}      # ISO 紧固件表
checks:
  bbox: {size: [=L, =W, =T], tol: 0.01}
  dims:
    - {name: pitch, value: "=dist(mount_holes.points[0], mount_holes.points[1])", expect: "=L-2*margin", tol: 0.01}
```

```bash
jig check  mount_plate.yaml            # 构建 + 自检（不写文件）
jig render mount_plate.yaml            # 多视图 PNG（iso/top/front/right，带标注和标尺）
jig build  mount_plate.yaml -f step,3mf  # 通过后导出并保存已验证版本
jig check  mount_plate.yaml -s L=150   # 参数试算
jig inspect mount_plate.yaml           # 每个特征的点位、包围盒、体积变化和可用锚点
jig log / jig restore                  # 已验证版本历史和回滚
jig promote mount_plate.yaml --name my_plate --expose L,W   # 沉淀为可复用组件
jig comp list | jig comp show hole | jig std M4
jig bench examples                     # 基准零件集通过率
```

## 文档

- [Skill（给 Agent 读的）](skill/jigcad/SKILL.md)，参考文档在 `skill/jigcad/references/`
- [设计说明、决策记录与路线图](docs/DESIGN.md)
- [试验：复刻 procgen-hy4 模型（含多材质 city-gate）](trials/procgen/REPORT.md)

## 开发

```bash
pip install -e '.[dev]'
pytest -q            # 核心测试 + 缺陷注入测试
```
