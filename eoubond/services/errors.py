class BusinessError(Exception):
    """A rule violation the user can fix. Message is safe to show in the UI."""


class ValidationError(BusinessError):
    pass


class BondError(BusinessError):
    pass


class InsufficientStock(BusinessError):
    def __init__(self, shortages: list[dict]):
        self.shortages = shortages
        parts = [
            f"{s['label']}: need {s['required']}, available {s['available']}, short {s['shortfall']}"
            for s in shortages
        ]
        super().__init__("Insufficient duty-saved stock - " + "; ".join(parts))


class UnmappedDescriptions(BusinessError):
    def __init__(self, lines):
        self.lines = lines
        descs = ", ".join(f"line {l.line_no} '{l.description}'" for l in lines)
        super().__init__(f"Map every description to an item code before posting: {descs}")
