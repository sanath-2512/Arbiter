package lib

import "testing"

func TestJudgeSpaces(t *testing.T) {
	if got := Initials("ada  lovelace"); got != "AL" {
		t.Fatalf("got %q", got)
	}
}
