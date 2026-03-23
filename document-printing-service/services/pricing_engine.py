from dataclasses import dataclass


@dataclass
class PricingResult:
    bw_pages: int
    color_pages: int
    bw_cost: float
    color_cost: float
    density_multiplier: float
    total_cost: float
    recommendation: str


def calculate_pricing(page_count: int, bw_pages: int, color_pages: int, color_density: float, copies: int, mode: str) -> PricingResult:
    copies = max(1, int(copies))
    page_count = max(1, int(page_count))
    bw_pages = max(0, int(bw_pages))
    color_pages = max(0, int(color_pages))
    color_density = max(0.0, float(color_density))

    bw_rate = 2.0
    base_color_rate = 6.0
    density_factor = round(max(0.5, color_density * 8), 2)

    if mode == "bw":
        bw_total_pages = page_count * copies
        bw_cost = bw_total_pages * bw_rate
        color_cost = 0.0
        total = bw_cost
        final_color_pages = 0
    else:
        # Use analysis-derived color pages directly to keep backend and UI preview aligned.
        effective_color_pages = min(page_count, color_pages)
        effective_bw_pages = max(0, page_count - effective_color_pages)

        bw_total_pages = effective_bw_pages * copies
        color_total_pages = effective_color_pages * copies
        bw_cost = bw_total_pages * bw_rate
        color_cost = color_total_pages * (base_color_rate + density_factor)
        total = bw_cost + color_cost
        final_color_pages = color_total_pages

    recommendation = "Black & White" if color_density < 0.08 else "Color"

    return PricingResult(
        bw_pages=bw_total_pages,
        color_pages=final_color_pages,
        bw_cost=round(bw_cost, 2),
        color_cost=round(color_cost, 2),
        density_multiplier=density_factor,
        total_cost=round(total, 2),
        recommendation=recommendation,
    )
