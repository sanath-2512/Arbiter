package lib

import "testing"

func TestInitials(t *testing.T) {
	if Initials("grace hopper") != "GH" {
		t.Fatal("gh")
	}
}
