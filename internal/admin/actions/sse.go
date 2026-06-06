package actions

import (
	"strconv"
	"strings"
)

// SSE wire format (text/event-stream) the Actions output panel consumes:
//
//	data: <line of stdout>\n\n
//	event: stderr\ndata: <line of stderr>\n\n
//	event: end\ndata: <exit code>\n\n
//
// The dispatcher emits these as the action runs; the host-dispatch path (CE
// SSH today, EE plugin RPC later) feeds the same frames.

// sseMessage formats one SSE message. Each line of data becomes its own data:
// field; a blank line terminates the message. An empty event is omitted.
func sseMessage(event, data string) string {
	var b strings.Builder
	if event != "" {
		b.WriteString("event: ")
		b.WriteString(event)
		b.WriteString("\n")
	}
	lines := strings.Split(data, "\n")
	for _, line := range lines {
		b.WriteString("data: ")
		b.WriteString(line)
		b.WriteString("\n")
	}
	b.WriteString("\n")
	return b.String()
}

// FormatStart is the initial event emitted before execution so the client
// knows the channel is open.
func FormatStart(actionName, email string) string {
	return sseMessage("start", "action="+actionName+" email="+email)
}

// FormatStdout frames one stdout line.
func FormatStdout(line string) string { return sseMessage("", line) }

// FormatStderr frames one stderr line.
func FormatStderr(line string) string { return sseMessage("stderr", line) }

// FormatEnd frames the terminal exit code (with an optional error detail).
func FormatEnd(exitCode int, errDetail string) string {
	if errDetail != "" {
		return sseMessage("end", strconv.Itoa(exitCode)+"\nerror: "+errDetail)
	}
	return sseMessage("end", strconv.Itoa(exitCode))
}

// BuildCommand assembles the forced-command request string: action name plus
// an optional payload, space-separated. The host's forced-command stanza
// interprets the whole string as $SSH_ORIGINAL_COMMAND, so a payload's shell
// metacharacters cannot smuggle extra commands past the dispatcher.
func BuildCommand(actionName, payload string) string {
	if payload == "" {
		return actionName
	}
	return actionName + " " + payload
}
