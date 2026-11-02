# Stage 2 出图渠道矩阵 v1（channels）

> 本文件定义 Stage 2 的多渠道契约。所有渠道**输入相同**（`03_prompts/prompts.json` + 参考图），**输出相同**（`04_covers/` 契约文件名 + 同一套后处理与质检），因此不拆 skill，只在本文件登记渠道能力。

## 1. 渠道能力矩阵

| 渠道 | 状态 | 执行方式 | 参考图传递 | 尺寸控制 | 适合场景 |
| --- | --- | --- | --- | --- | --- |
| `doubao-jimeng` | ✅ 当前默认 | 豆包对话 + 即梦插件，人工/半自动 | 用户手动上传 `01_input/reference.*` | 出图后需校验并重缩到精确像素 | 无 API 凭证；人工挑选质量 |
| `volcano-ark` | 🔲 预留接口 | 火山方舟图像 API，程序化 | API 参考图参数（i2i） | API 指定尺寸 + 本地 LANCZOS 校准 | 批量、无人值守 |
| `trae-host` | ✅ 可用 | Trae 本地执行：宿主图像能力或 Stage 2b Pillow 兜底 | 直接读文件 | 代码强制精确像素 | 豆包不可用时的本地兜底 |

## 2. 统一输入

```json
{
  "prompt_pack": "03_prompts/prompts.json",
  "reference_image": "01_input/reference.png",
  "slots": [
    {"slot": 1, "file": "cover_01_hit.png",      "size": [2848, 1600], "composition": "爆点仰拍"},
    {"slot": 2, "file": "cover_02_contrast.png", "size": [2848, 1600], "composition": "冷暖对比"},
    {"slot": 3, "file": "cover_03_guard.png",    "size": [2848, 1600], "composition": "守护挡身"},
    {"slot": "vertical", "file": "cover_04_vertical.png", "size": [1080, 1920], "composition": "胸像逆光"}
  ]
}
```

提示词结构见 `prompt-spec.md`；构图差异硬约束见 `cover-spec.md`。

## 3. 统一输出与后处理（任何渠道都必过）

1. 文件命名严格按上表，jpg/png 均可；
2. 精确尺寸断言（不是精确像素 → `PIL Image.LANCZOS` 重缩，拒绝近似）；
3. 四项质检：无文字 / 无武器 / 无儿童 / 无血腥，任一 Fail 重做该张；
4. 三张横版构图肉眼可分；
5. 写回 `job.json`：`cover_map[].image` / `vertical_cover.image` + `channel`（本表渠道名）+ `status="done"`，hash 由有执行能力的宿主补算。

## 4. Adapter 接口（程序化渠道专用）

```python
def generate(prompt_pack: dict, reference_image: str, out_dir: str) -> list[str]:
    """输入提示词包 + 参考图，输出 4 个契约文件路径。渠道内部负责重试与下载。"""
```

- `volcano-ark` 接入时新建 `lib/cover_channel_volcano.py` 实现该签名，不动上层；
- `doubao-jimeng` 无代码（人工交接），`trae-host` 由 Stage 2b Pillow 路径承担，不需要该接口。

## 5. 渠道选择规则

`job.json.prompts.channel` 记录实际使用渠道；缺省 `doubao-jimeng`。同一任务允许分渠道混出（如横版 API、竖版人工），但每张图的 `channel` 必须如实记录，便于复盘按渠道统计失败模式（写入 `failure-cases.md`）。
