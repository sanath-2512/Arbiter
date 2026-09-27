package lib

import "testing"

func TestJudgeEmpty(t *testing.T) {
	if m, ok := Max(nil); ok || m != 0 {
		t.Fatalf("got %d %v", m, ok)
	}
}
