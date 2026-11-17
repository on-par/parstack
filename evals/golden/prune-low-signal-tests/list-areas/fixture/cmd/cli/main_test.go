package main

import "testing"

func TestParsesFlags(t *testing.T) {
	if got := parse([]string{"-v"}); !got.Verbose {
		t.Fatal("expected verbose")
	}
}

func TestDefaultsToQuiet(t *testing.T) {
	if got := parse(nil); got.Verbose {
		t.Fatal("expected quiet")
	}
}
