package rates

import (
	"os"
	"testing"
)

func TestAppliesRate(t *testing.T) {
	if Apply(100, 0.2) != 120 {
		t.Fatal("rate not applied")
	}
}

func TestLiveRates(t *testing.T) {
	if os.Getenv("RATES_API") == "" {
		t.Skip("needs RATES_API")
	}
	if len(Fetch()) == 0 {
		t.Fatal("no rates")
	}
}
