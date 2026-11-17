import sys

from calc import add

got = add(2, 3)
if got != 5:
    print(f"FAIL add(2, 3) = {got}, want 5")
    sys.exit(1)
print("ok add(2, 3) = 5")
