package lib

import "testing"

func TestMax(t *testing.T) {
	if m, _ := Max([]int{1, 5, 2}); m != 5 {
		t.Fatal(m)
	}
}
