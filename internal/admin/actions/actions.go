// Package actions is the catena-admin Actions/Recovery catalog + the host
// dispatch machinery. The static catalog (admin-actions.yml) holds the CE
// actions; EE actions are contributed at runtime by license-gated plugins and
// merged in by the route (see MergedCatalog), so an EE button cannot appear
// without its plugin loaded. Ported from the Python catena_admin.actions.
package actions

import (
	"os"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// ActionsTabCategories is the fixed display order of the Actions-tab sub-nav.
// A catalog category outside this set (other than Recovery) buckets into "Ops"
// so a YAML typo cannot silently drop a button.
var ActionsTabCategories = []string{"Upgrades", "Backups", "Initial apps setup", "Ops"}

// RecoveryCategory routes an action to the Recovery tab instead of Actions.
const RecoveryCategory = "Recovery"

// ActionArgument is an argument schema declared on an action (e.g. the
// passphrase on generate-recovery-archive).
type ActionArgument struct {
	Name string
	Type string // text | password | select | ...
}

// Action is one catalog row. Source marks where it came from: "ce" for the
// static catalog, or a plugin id for an EE-contributed action.
type Action struct {
	Name      string
	Title     string
	TitleFR   string
	Category  string
	Icon      string
	Timeout   int
	Shell     string
	Arguments []ActionArgument
	Source    string
}

// LocalizedTitle returns the action title for locale, falling back to EN.
func (a Action) LocalizedTitle(locale string) string {
	if locale == "fr" && a.TitleFR != "" {
		return a.TitleFR
	}
	return a.Title
}

// CategorySlug is the stable i18n key slug for a category name.
func (a Action) CategorySlug() string {
	return strings.ReplaceAll(strings.ToLower(a.Category), " ", "-")
}

type catalogFile struct {
	Actions []struct {
		Name      string `yaml:"name"`
		Title     string `yaml:"title"`
		TitleFR   string `yaml:"title_fr"`
		Category  string `yaml:"category"`
		Icon      string `yaml:"icon"`
		Timeout   int    `yaml:"timeout"`
		Shell     string `yaml:"shell"`
		Arguments []struct {
			Name string `yaml:"name"`
			Type string `yaml:"type"`
		} `yaml:"arguments"`
	} `yaml:"actions"`
}

// Load reads and normalizes the YAML catalog. A missing/invalid file returns
// nil so the route renders an empty-state rather than erroring.
func Load(path string) []Action {
	if path == "" {
		path = envOr("CATENA_ADMIN_ACTIONS_FILE", "/etc/catena/admin-actions.yml")
	}
	raw, err := os.ReadFile(path) // #nosec G304 -- operator-configured catalog path (env or fixed default), not request input
	if err != nil {
		return nil
	}
	var data catalogFile
	if err := yaml.Unmarshal(raw, &data); err != nil {
		return nil
	}
	var out []Action
	for _, e := range data.Actions {
		name := strings.TrimSpace(e.Name)
		if name == "" {
			continue
		}
		var args []ActionArgument
		for _, a := range e.Arguments {
			if a.Name == "" {
				continue
			}
			typ := a.Type
			if typ == "" {
				typ = "text"
			}
			args = append(args, ActionArgument{Name: a.Name, Type: typ})
		}
		out = append(out, Action{
			Name:      name,
			Title:     firstNonEmpty(e.Title, name),
			TitleFR:   e.TitleFR,
			Category:  normalizeCategory(e.Category),
			Icon:      e.Icon,
			Timeout:   e.Timeout,
			Shell:     e.Shell,
			Arguments: args,
			Source:    "ce",
		})
	}
	return out
}

// NormalizeCategory maps a raw category onto the known set (unknown ->
// "Ops", Recovery preserved). Exported so plugin-contributed actions get the
// same normalization the YAML catalog gets before MergedCatalog.
func NormalizeCategory(raw string) string { return normalizeCategory(raw) }

func normalizeCategory(raw string) string {
	v := strings.TrimSpace(raw)
	if v == "" {
		return "Ops"
	}
	if v == RecoveryCategory {
		return RecoveryCategory
	}
	for _, c := range ActionsTabCategories {
		if c == v {
			return v
		}
	}
	return "Ops"
}

// ForActionsTab filters to actions that render on the Actions tab.
func ForActionsTab(actions []Action) []Action {
	var out []Action
	for _, a := range actions {
		if a.Category != RecoveryCategory {
			out = append(out, a)
		}
	}
	return out
}

// ForRecoveryTab filters to actions that render on the Recovery tab.
func ForRecoveryTab(actions []Action) []Action {
	var out []Action
	for _, a := range actions {
		if a.Category == RecoveryCategory {
			out = append(out, a)
		}
	}
	return out
}

// CategoryGroup is one Actions-tab category bucket.
type CategoryGroup struct {
	Category string
	Actions  []Action
}

// Slug is the stable i18n key slug for the group's category.
func (g CategoryGroup) Slug() string {
	return strings.ReplaceAll(strings.ToLower(g.Category), " ", "-")
}

// GroupByCategory buckets actions into the fixed category order. Empty
// categories are still returned (empty slice) so the sub-nav stays stable.
func GroupByCategory(actions []Action) []CategoryGroup {
	buckets := map[string][]Action{}
	for _, c := range ActionsTabCategories {
		buckets[c] = nil
	}
	for _, a := range actions {
		if _, ok := buckets[a.Category]; ok {
			buckets[a.Category] = append(buckets[a.Category], a)
		}
	}
	out := make([]CategoryGroup, 0, len(ActionsTabCategories))
	for _, c := range ActionsTabCategories {
		out = append(out, CategoryGroup{Category: c, Actions: buckets[c]})
	}
	return out
}

// MergedCatalog combines the static CE catalog with EE actions contributed by
// loaded plugins. Plugin actions are appended after the CE ones and sorted by
// name within their category by GroupByCategory. Today pluginActions is empty
// (the plugin SDK gains Actions() in M2.5); the merge seam is here so wiring
// it in later is a one-liner, not a rework.
func MergedCatalog(static []Action, pluginActions []Action) []Action {
	out := append([]Action(nil), static...)
	out = append(out, pluginActions...)
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Category != out[j].Category {
			return out[i].Category < out[j].Category
		}
		return out[i].Name < out[j].Name
	})
	return out
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func envOr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}
