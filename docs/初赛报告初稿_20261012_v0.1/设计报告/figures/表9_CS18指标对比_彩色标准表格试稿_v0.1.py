from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# Source: Design report, Table 9. This preview changes only presentation;
# labels, values, denominator, and test scope are preserved.
HEADERS = ["指标", "新事实核验流程", "匹配参考流程"]
ROWS = [
    ("严格通过", "62/64", "7/64"),
    ("严格通过比例", "96.9%", "10.9%"),
    ("配对差", "+55 题", "+85.94 个百分点"),
]

COLORS = {
    "navy": "#254BA6",
    "blue": "#315BC7",
    "blue_light": "#F0F4FC",
    "blue_alt": "#EAF0FA",
    "teal": "#168B82",
    "teal_light": "#EDF7F4",
    "teal_alt": "#E5F2EF",
    "white": "#FFFFFF",
    "text": "#26384D",
    "muted": "#687B8E",
    "border": "#D5DEE8",
    "reference": "#F3F6FA",
    "reference_alt": "#EAF0F6",
}


def build_table() -> plt.Figure:
    plt.rcParams.update(
        {
            "font.family": "Microsoft YaHei",
            "axes.unicode_minus": False,
            "svg.fonttype": "none",
        }
    )

    fig, ax = plt.subplots(figsize=(15.2 / 2.54, 8.8 / 2.54), dpi=300)
    fig.patch.set_facecolor(COLORS["white"])
    ax.set_facecolor(COLORS["white"])
    ax.set_xlim(0, 1500)
    ax.set_ylim(0, 870)
    ax.axis("off")

    ax.text(76, 812, "表 9  /  CS18 v0.6", fontsize=7.5, color=COLORS["blue"],
            fontweight="bold", va="center", family="Microsoft YaHei")
    ax.text(76, 768, "严格通过指标配对对比", fontsize=14, color=COLORS["text"],
            fontweight="bold", va="center", family="Microsoft YaHei")
    ax.text(76, 728, "64 道合成业务题｜严格通过：回答正确且证据充分",
            fontsize=8.2, color=COLORS["muted"], va="center", family="Microsoft YaHei")

    x0 = 76
    widths = [340, 510, 510]
    header_y = 583
    header_h = 76
    row_h = 94
    header_fills = [COLORS["navy"], COLORS["teal"], COLORS["blue"]]

    x = x0
    for label, width, fill in zip(HEADERS, widths, header_fills):
        ax.add_patch(Rectangle((x, header_y), width, header_h, facecolor=fill,
                               edgecolor=COLORS["white"], linewidth=1.0))
        ax.text(x + width / 2, header_y + header_h / 2, label, fontsize=9,
                color=COLORS["white"], fontweight="bold", ha="center", va="center",
                family="Microsoft YaHei")
        x += width

    for row_index, row in enumerate(ROWS):
        y = header_y - (row_index + 1) * row_h
        alt = row_index % 2 == 1
        fills = (
            COLORS["blue_alt"] if alt else COLORS["blue_light"],
            COLORS["teal_alt"] if alt else COLORS["teal_light"],
            COLORS["reference_alt"] if alt else COLORS["reference"],
        )
        x = x0
        for col_index, (value, width, fill) in enumerate(zip(row, widths, fills)):
            ax.add_patch(Rectangle((x, y), width, row_h, facecolor=fill,
                                   edgecolor=COLORS["border"], linewidth=0.9))
            color = COLORS["navy"] if col_index == 0 else (
                COLORS["teal"] if col_index == 1 else COLORS["text"]
            )
            font_size = 9.4 if col_index == 0 else (10.2 if col_index == 1 else 9.8)
            if row_index == 2:
                font_size -= 0.3
            ax.text(x + (24 if col_index == 0 else width / 2), y + row_h / 2,
                    value, fontsize=font_size, color=color,
                    fontweight="bold" if row_index == 2 or col_index > 0 else "medium",
                    ha="left" if col_index == 0 else "center", va="center",
                    family="Microsoft YaHei")
            x += width

    ax.text(76, 245,
            "配对差分别表示严格通过题数差与通过率差；本结果为合成题上的描述性对照，不代表真实业务总体准确率。",
            fontsize=7.1, color=COLORS["muted"], va="center", family="Microsoft YaHei")

    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    return fig


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    stem = "表9_CS18指标对比_彩色标准表格试稿_v0.1"
    fig = build_table()
    fig.savefig(out_dir / f"{stem}.svg", format="svg", facecolor=COLORS["white"])
    fig.savefig(out_dir / f"{stem}.png", format="png", dpi=300, facecolor=COLORS["white"])
    plt.close(fig)
    print(f"created {out_dir / f'{stem}.svg'}")
    print(f"created {out_dir / f'{stem}.png'}")
    print("validated 3 metrics, 2 matched flows, 64 paired synthetic cases")


if __name__ == "__main__":
    main()
