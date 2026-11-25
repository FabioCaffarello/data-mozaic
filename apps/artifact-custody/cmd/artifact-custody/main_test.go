package main

import "testing"

func TestValidateTransferBudget(t *testing.T) {
	const mib = int64(1024 * 1024)
	if err := validateTransferBudget(100*mib, 4, 420*mib); err != nil {
		t.Fatalf("configured local budget should pass: %v", err)
	}
	if err := validateTransferBudget(100*mib, 5, 420*mib); err == nil {
		t.Fatal("budget smaller than concurrent maximum spools was accepted")
	}
	if err := validateTransferBudget(101*mib, 1, 420*mib); err == nil {
		t.Fatal("artifact limit above the implementation cap was accepted")
	}
}
