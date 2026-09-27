package lib

import "testing"

func TestASCII(t *testing.T) {
	if Reverse("ab") != "ba" {
		t.Fatal("ascii")
	}
}
