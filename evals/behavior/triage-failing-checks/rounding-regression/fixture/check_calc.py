import sys

from calc import total_cents

got = total_cents([1.15, 0.57])
if got != 172:
    print(f"FAIL total_cents: want 172, got {got}")
    sys.exit(1)
print("ok")
