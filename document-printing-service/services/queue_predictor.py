
def predict_queue_time_minutes(page_count: int, color_density: float, queue_load: int, copy_count: int) -> int:
    page_component = page_count * copy_count * 0.35
    color_component = (color_density * 100) * 0.08
    queue_component = queue_load * 2.5
    predicted = 1 + page_component + color_component + queue_component
    return max(1, int(round(predicted)))


def derive_queue_priority(predicted_minutes: int) -> str:
    if predicted_minutes <= 5:
        return "high"
    if predicted_minutes <= 15:
        return "medium"
    return "normal"
