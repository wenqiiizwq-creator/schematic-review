"""Structural checks for actionable repair instructions, not electrical approval."""

READINESS = ("READY", "CONDITIONAL", "DESIGN_REQUIRED")
STEP_KINDS = ("CONNECT", "COMPONENT", "ASSEMBLY", "DOCUMENT", "DESIGN")
STAGES = ("NETLIST", "CALCULATION", "DOCUMENT", "BENCH", "PCB")


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_remediation(fid, value, finding_ids):
    errors = []

    def require(ok, message):
        if not ok:
            errors.append(f"{fid}.remediation: {message}")

    def fields(item, names, label):
        require(all(nonempty(item.get(k)) for k in names), f"{label} needs {'/'.join(names)}")

    def rows(name, required=False):
        items = value.get(name)
        require(isinstance(items, list), f"{name} must be an array")
        if not isinstance(items, list):
            return []
        require(not required or bool(items), f"{name} must not be empty")
        require(all(isinstance(x, dict) for x in items), f"{name} needs objects")
        return [x for x in items if isinstance(x, dict)]

    if not isinstance(value, dict):
        return [f"{fid}.remediation: detailed instructions required"]
    readiness = value.get("readiness")
    require(readiness in READINESS, "invalid readiness")
    fields(value, ("purpose", "impact_review"), "repair")
    prerequisites = rows("prerequisites")
    for item in prerequisites:
        fields(item, ("input", "reason", "how_to_obtain", "acceptance"), "prerequisite")
    if readiness == "READY":
        require(not prerequisites, "READY cannot have unresolved prerequisites")
    elif readiness in ("CONDITIONAL", "DESIGN_REQUIRED"):
        require(bool(prerequisites), "conditional/design instructions need unresolved inputs and closure methods")

    steps = rows("steps", required=True)
    for number, item in enumerate(steps, 1):
        label = f"step {number}"
        fields(item, ("target", "before", "after", "instruction"), label)
        require(item.get("kind") in STEP_KINDS, f"{label}: invalid kind")
        if readiness == "READY":
            require(item.get("kind") != "DESIGN", "READY cannot still require a design decision")
        if item.get("kind") == "CONNECT":
            connections = []
            for key in ("remove_connections", "add_connections"):
                entries = item.get(key)
                require(isinstance(entries, list), f"{label}: {key} must be an array")
                if not isinstance(entries, list):
                    continue
                connections.extend(entries)
                for edge in entries:
                    require(isinstance(edge, dict) and all(nonempty(edge.get(k)) for k in ("from", "to")),
                            f"{label}: connection needs both endpoints")
            require(bool(connections), f"{label}: CONNECT needs an actual removed/added connection")

    parameters = rows("parameters")
    if any(x.get("kind") in ("COMPONENT", "ASSEMBLY") for x in steps):
        require(bool(parameters), "component/assembly edit needs explicit specifications")
    for item in parameters:
        fields(item, ("target", "specification"), "parameter")
        status = item.get("status")
        require(status in ("SELECTED", "CANDIDATE", "TBD"), "invalid parameter status")
        basis = item.get("basis")
        require(isinstance(basis, list) and bool(basis) and all(
            isinstance(x, dict) and all(nonempty(x.get(k)) for k in ("source", "locator"))
            for x in basis), "parameter needs locatable basis")
        if status in ("CANDIDATE", "TBD"):
            fields(item, ("needed_input", "selection_method"), "unresolved parameter")
            require(bool(prerequisites), "unresolved parameter needs a prerequisite")
            require(readiness != "READY", "READY cannot contain candidate/TBD parameters")

    related = value.get("related_findings")
    require(isinstance(related, list) and all(nonempty(x) and x in finding_ids and x != fid for x in related),
            "related_findings must reference other existing finding IDs")
    verification = rows("verification", required=True)
    for item in verification:
        fields(item, ("method", "expected"), "verification")
        require(item.get("stage") in STAGES, "invalid verification stage")
    require(any(x.get("stage") in ("NETLIST", "CALCULATION", "DOCUMENT") for x in verification),
            "needs an editing/calculation acceptance check, not only future bench/PCB testing")
    return errors
