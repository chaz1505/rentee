from datetime import timedelta


def generate_candidate_slots(
    viewing_request_id,
    listing_id,
    listing_name,
    availability_start,
    availability_end,
    tenant_start,
    tenant_end,
    duration_minutes=30,
    interval_minutes=30,
    priority=0,
):
    """Generate deterministic slots contained by both availability windows."""
    if any(
        value.tzinfo is None
        for value in (
            availability_start, availability_end, tenant_start, tenant_end
        )
    ):
        raise ValueError("Scheduling datetimes must be timezone-aware.")
    if duration_minutes <= 0 or interval_minutes <= 0:
        raise ValueError("Duration and candidate interval must be positive.")
    if availability_end <= availability_start or tenant_end <= tenant_start:
        return []

    duration = timedelta(minutes=duration_minutes)
    interval = timedelta(minutes=interval_minutes)
    slots = []
    candidate_start = availability_start
    while candidate_start + duration <= availability_end:
        candidate_end = candidate_start + duration
        if candidate_start >= tenant_start and candidate_end <= tenant_end:
            slots.append({
                "viewing_request_id": viewing_request_id,
                "listing_id": listing_id,
                "listing_name": listing_name,
                "start": candidate_start,
                "end": candidate_end,
                "priority": priority,
            })
        candidate_start += interval
    return slots


def _schedule_metrics(appointments):
    ordered = sorted(
        appointments,
        key=lambda item: (
            item["start"], item["end"], item["viewing_request_id"]
        ),
    )
    idle = sum(
        int((later["start"] - earlier["end"]).total_seconds() // 60)
        for earlier, later in zip(ordered, ordered[1:])
    )
    span = (
        int((ordered[-1]["end"] - ordered[0]["start"]).total_seconds() // 60)
        if ordered
        else 0
    )
    return ordered, idle, span


def optimise_schedule(
    candidates,
    tenant_start,
    tenant_end,
    travel_buffer_minutes=15,
):
    """Choose a deterministic lexicographically optimal non-overlapping schedule."""
    if tenant_start.tzinfo is None or tenant_end.tzinfo is None:
        raise ValueError("Tenant window must be timezone-aware.")
    if tenant_end <= tenant_start:
        raise ValueError("Tenant window end must be after its start.")
    if travel_buffer_minutes < 0:
        raise ValueError("Travel buffer cannot be negative.")

    grouped = {}
    for candidate in candidates:
        if candidate["start"].tzinfo is None or candidate["end"].tzinfo is None:
            raise ValueError("Candidate datetimes must be timezone-aware.")
        if (
            candidate["start"] < tenant_start
            or candidate["end"] > tenant_end
            or candidate["end"] <= candidate["start"]
        ):
            continue
        grouped.setdefault(candidate["viewing_request_id"], []).append(candidate)

    request_ids = sorted(grouped)
    for request_id in request_ids:
        grouped[request_id].sort(
            key=lambda item: (
                item["start"], item["end"], item.get("listing_id", "")
            )
        )

    buffer = timedelta(minutes=travel_buffer_minutes)
    best = []
    best_objective = None
    best_signature = None

    def compatible(selected, candidate):
        ordered = sorted(selected + [candidate], key=lambda item: item["start"])
        return all(
            later["start"] >= earlier["end"] + buffer
            for earlier, later in zip(ordered, ordered[1:])
        )

    def consider(selected):
        nonlocal best, best_objective, best_signature
        ordered, idle, span = _schedule_metrics(selected)
        objective = (
            len(ordered),
            sum(float(item.get("priority", 0) or 0) for item in ordered),
            -idle,
            -span,
        )
        signature = tuple(
            (item["start"].isoformat(), item["viewing_request_id"])
            for item in ordered
        )
        if (
            best_objective is None
            or objective > best_objective
            or (objective == best_objective and signature < best_signature)
        ):
            best = list(ordered)
            best_objective = objective
            best_signature = signature

    def search(position, selected):
        if position == len(request_ids):
            consider(selected)
            return
        request_id = request_ids[position]
        search(position + 1, selected)
        for candidate in grouped[request_id]:
            if compatible(selected, candidate):
                search(position + 1, selected + [candidate])

    search(0, [])
    appointments, idle_minutes, _span_minutes = _schedule_metrics(best)
    return {
        "appointments": appointments,
        "scheduled_count": len(appointments),
        "idle_minutes": idle_minutes,
    }
