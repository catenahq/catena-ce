package auditlog

import "testing"

func TestIdentifierIsStable(t *testing.T) {
	if Identifier != "catena-admin" {
		t.Fatalf("Identifier must stay catena-admin (the catena-ee audit plugin matches it), got %q", Identifier)
	}
}

func TestEmitIsNoOpWithoutJournald(t *testing.T) {
	// On CI / dev there is no journald socket; Emit must not panic or block.
	Emit("backup-now", "op@example.com", "1.2.3.4", 0)
	Emit("rollback", "", "", 3)
}
