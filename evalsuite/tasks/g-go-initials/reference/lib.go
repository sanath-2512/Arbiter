package lib

import "strings"

// Initials returns the upper-case first letter of each word.
func Initials(name string) string {
	var b strings.Builder
	for _, w := range strings.Fields(name) {
		b.WriteString(strings.ToUpper(w[:1]))
	}
	return b.String()
}
