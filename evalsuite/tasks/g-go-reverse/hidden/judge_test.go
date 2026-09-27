package lib

import "testing"

func TestJudgeUnicode(t *testing.T) {
	if got := Reverse("héllo"); got != "olléh" {
		t.Fatalf("got %q", got)
	}
}
