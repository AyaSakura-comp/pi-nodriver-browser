#!/usr/bin/env python3
import matplotlib.pyplot as plt
import matplotlib.patches as patches

# Configure CJK Font
plt.rcParams['font.sans-serif'] = ['Noto Sans CJK TC', 'WenQuanYi Zen Hei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8.5), gridspec_kw={'height_ratios': [2.2, 1]}, facecolor='#0f172a')

# Palette
DARK_BG = '#0f172a'
CARD_BG = '#1e293b'
TEXT_COLOR = '#f8fafc'
MUTED_TEXT = '#94a3b8'

C_PLAN = '#38bdf8'       # Qwen planner (compact)
C_ACT = '#34d399'        # Nodriver actuation / DOM
C_VERIFY = '#a855f7'     # LLM Verify Done
C_OPEN = '#f59e0b'       # Open page

# Steps data: (step_index, label, start_ms, duration_ms, category, detail)
steps_data = [
    (0, "S0: Open", 0, 450, C_OPEN),
    (1, "S1: Plan", 450, 659, C_PLAN),
    (1, "S1: Click", 1109, 550, C_ACT),
    (2, "S2: Plan", 1659, 654, C_PLAN),
    (2, "S2: Fill", 2313, 480, C_ACT),
    (3, "S3: Plan", 2793, 553, C_PLAN),
    (3, "S3: Verify", 3346, 520, C_ACT),
    (4, "S4: Plan", 3866, 568, C_PLAN),
    (4, "S4: Crystal", 4434, 510, C_ACT),
    (5, "S5: Plan", 4944, 546, C_PLAN),
    (5, "S5: Check", 5490, 480, C_ACT),
    (6, "S6: Plan", 5970, 562, C_PLAN),
    (6, "S6: Teleport", 6532, 530, C_ACT),
    (7, "S7: Plan", 7062, 633, C_PLAN),
    (7, "S7: Core", 7695, 540, C_ACT),
    (8, "S8: Plan", 8235, 587, C_PLAN),
    (8, "S8: Done", 8822, 928, C_VERIFY)
]

TOTAL_MS = 9750

# Track 1: Autopilot Execution Pipeline
ax1.set_facecolor(CARD_BG)
ax1.set_xlim(-150, 10100)
ax1.set_ylim(-0.6, 8.6)

y_labels = [
    "Step 0: Open URL (450ms)",
    "Step 1: Level 1 解開蒼藍 (1209ms)",
    "Step 2: Level 2 輸入通行碼 (1134ms)",
    "Step 3: Level 2 驗證紫晶 (1073ms)",
    "Step 4: Level 3 翡翠之心 (1078ms)",
    "Step 5: Level 4 解除防護 (1026ms)",
    "Step 6: Level 4 啟動傳送 (1092ms)",
    "Step 7: Level 5 緋紅核銷 (1173ms)",
    "Step 8: Plan Done & Verify (1515ms)"
]

for s_idx, label, start, dur, color in steps_data:
    y = s_idx
    rect = patches.FancyBboxPatch((start, y - 0.32), dur, 0.64,
                                  boxstyle="round,pad=0.02,rounding_size=40",
                                  facecolor=color, edgecolor='#0f172a', alpha=0.9, lw=1.2)
    ax1.add_patch(rect)
    
    text_color = '#ffffff' if color in (C_VERIFY, C_PLAN) else '#0f172a'
    if dur > 500:
        ax1.text(start + dur / 2, y, f"{dur}ms", ha='center', va='center',
                 color=text_color, fontsize=9.5, fontweight='bold')
    else:
        ax1.text(start + dur / 2, y, f"{dur}m", ha='center', va='center',
                 color=text_color, fontsize=8.5, fontweight='bold')

ax1.set_yticks(range(9))
ax1.set_yticklabels(y_labels, color=TEXT_COLOR, fontsize=10.5, fontweight='600')
ax1.set_title("五重嵌套 Iframe 闖關基準測試：時間拆解分析 (Time Breakdown Analysis)\n總耗時 9.75 秒 (8 步自主闖關全自動連鎖通關)",
              color=TEXT_COLOR, fontsize=13.5, fontweight='bold', pad=12)
ax1.tick_params(colors=MUTED_TEXT, labelsize=9.5)
ax1.grid(axis='x', color='#334155', linestyle='--', alpha=0.5)

# Track 2: Nested Iframe Depth & Hierarchy
ax2.set_facecolor(CARD_BG)
ax2.set_xlim(-150, 10100)
ax2.set_ylim(-0.6, 5.6)

depth_segments = [
    ("Root Document (Depth 0)", 0, 450, '#64748b'),
    ("第一重結界 (Depth 1)", 450, 1209, '#3b82f6'),
    ("第二重結界 (Depth 2)", 1659, 2207, '#8b5cf6'),
    ("第三重結界 (Depth 3)", 3866, 1078, '#10b981'),
    ("第四重結界 (Depth 4)", 4944, 2118, '#f59e0b'),
    ("第五重結界 (Depth 5)", 7062, 1173, '#f43f5e'),
    ("Victory State (Root HUD)", 8235, 1515, '#22c55e')
]

depth_y = [0, 1, 2, 3, 4, 5, 0]

for i, (name, start, dur, col) in enumerate(depth_segments):
    y = depth_y[i]
    rect = patches.FancyBboxPatch((start, y - 0.35), dur, 0.7,
                                  boxstyle="round,pad=0.02,rounding_size=50",
                                  facecolor=col, edgecolor='#0f172a', alpha=0.9, lw=1.2)
    ax2.add_patch(rect)
    ax2.text(start + dur / 2, y, name, ha='center', va='center',
             color='#ffffff', fontsize=9, fontweight='bold')

ax2.set_yticks(range(6))
ax2.set_yticklabels(["Root / HUD", "Depth 1 (蒼藍)", "Depth 2 (紫晶)", "Depth 3 (翡翠)", "Depth 4 (黃金)", "Depth 5 (緋紅)"],
                    color=TEXT_COLOR, fontsize=10, fontweight='600')
ax2.set_xlabel("累計執行時間 (Milliseconds / 毫秒)", color=TEXT_COLOR, fontsize=11, fontweight='bold')
ax2.tick_params(colors=MUTED_TEXT, labelsize=9.5)
ax2.grid(axis='x', color='#334155', linestyle='--', alpha=0.5)

# Legend
legend_patches = [
    patches.Patch(color=C_OPEN, label="頁面導航 / Open (450ms)"),
    patches.Patch(color=C_PLAN, label="Qwen 輕量規劃 / Planner (平均 ~590ms/步)"),
    patches.Patch(color=C_ACT, label="DOM 執行與座標累積 / Actuate (~510ms/步)"),
    patches.Patch(color=C_VERIFY, label="結果驗證 / Verify Done")
]
ax1.legend(handles=legend_patches, loc='upper right', facecolor='#0f172a', edgecolor='#334155',
           labelcolor=TEXT_COLOR, fontsize=9.5)

plt.tight_layout()
output_path = "/home/chihmin/.gemini/antigravity-cli/brain/7c979a18-7e7a-42b8-afa9-0bc28a9e6299/nested-5iframe-time-breakdown.png"
plt.savefig(output_path, dpi=180, facecolor='#0f172a', edgecolor='none')
print(f"Chart saved to {output_path}")
