package lib

import "testing"

func TestPositive(t *testing.T) {
	if SumPositive([]int{1, 2}) != 3 {
		t.Fatal("sum")
	}
}
