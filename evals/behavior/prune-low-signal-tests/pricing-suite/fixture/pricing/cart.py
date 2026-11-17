class Cart:
    def __init__(self, discount=0.0):
        self._items = []
        self.discount = discount

    def add(self, name, price, qty=1):
        if qty <= 0:
            raise ValueError("quantity must be positive")
        self._items.append((name, price, qty))

    def total(self):
        gross = sum(price * qty for _, price, qty in self._items)
        return round(gross * (1 - self.discount), 2)

    def receipt(self, card_number):
        lines = [f"{qty} x {name}: {price * qty:.2f}" for name, price, qty in self._items]
        lines.append(f"total: {self.total():.2f}")
        lines.append(f"paid with card ending {card_number[-4:]}")
        return "\n".join(lines)
