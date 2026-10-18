# 5-Layer Nested Iframe Benchmark (五重嵌套 Iframe 闖關基準測試)

This benchmark evaluates autonomous browser agents on deep DOM traversal, nested iframe coordinate accumulation, frame-aware candidate deduplication, and sequential multi-step problem solving.

## Structure
- `index.html`: Outer HUD & progress tracker (Depth 0)
- `level1.html`: Level 1 Sapphire Sanctuary (Depth 1) - Generates passcode `SAPPHIRE-8899`
- `level2.html`: Level 2 Amethyst Corridor (Depth 2) - Input field + verification button
- `level3.html`: Level 3 Emerald Glade (Depth 3) - 3-crystal array; target is "翡翠之心"
- `level4.html`: Level 4 Golden Foundry (Depth 4) - Checkbox "解除安全防護" + button "啟動超載傳送"
- `level5.html`: Level 5 Crimson Core (Depth 5) - Final button "終極核銷核心" revealing `FLAG{DEEP_IFRAME_5X_SUCCESS_2026}`

## How to Run
```bash
python3 serve.py
# Serves at http://127.0.0.1:8899/index.html
```
