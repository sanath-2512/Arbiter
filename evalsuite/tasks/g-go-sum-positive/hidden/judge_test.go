package lib

import "testing"

func TestJudgeNegative(t *testing.T) {
	if got := SumPositive([]int{-1, 2, 3}); got != 5 {
		t.Fatalf("got %d", got)
	}
}
