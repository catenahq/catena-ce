// Package auditlog emits catena-admin administrative actions to the systemd
// journal as structured entries. The audit trail IS journald
// (SYSLOG_IDENTIFIER=catena-admin); the EE audit panel reads it back with
// journalctl and the EE collector ships it off-box. On a host without journald
// (dev, non-systemd, CI) Emit is a silent no-op so the shell runs anywhere.
package auditlog

import (
	"fmt"
	"strconv"

	"github.com/coreos/go-systemd/v22/journal"
)

// Identifier is the journald SYSLOG_IDENTIFIER every audit row carries, so the
// panel/collector select with `journalctl -t catena-admin`. The catena-ee
// audit plugin must match this string.
const Identifier = "catena-admin"

// Emit records one dispatched admin action with queryable fields
// (`journalctl CATENA_ACTION=backup-now`). Best-effort: a journald error, or a
// host without journald, is swallowed -- auditing must never block or fail an
// action.
func Emit(action, email, ip string, rc int) {
	if !journal.Enabled() {
		return
	}
	priority := journal.PriInfo
	if rc != 0 {
		priority = journal.PriWarning
	}
	who := email
	if who == "" {
		who = "unknown"
	}
	_ = journal.Send(
		fmt.Sprintf("admin action %q by %s rc=%d", action, who, rc),
		priority,
		map[string]string{
			"SYSLOG_IDENTIFIER": Identifier,
			"CATENA_ACTION":     action,
			"CATENA_EMAIL":      email,
			"CATENA_IP":         ip,
			"CATENA_RC":         strconv.Itoa(rc),
		},
	)
}
