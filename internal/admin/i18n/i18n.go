// Package i18n is the catena-admin shell's bilingual EN/FR support, ported
// from the Python catena_admin.i18n. Translation source of truth is
// translations/{en,fr}.yml (nested YAML); keys are dotted paths
// (e.g. "tabs.apps"); values may carry {name} interpolation placeholders.
//
// Loader reads both files at startup into flattened dotted-key maps. A miss
// in the target locale falls back to EN (logged to stderr); a miss in all
// locales returns the key itself so a forgotten string is visible in the UI
// rather than rendering empty. The parity test asserts fr has every key en
// has, so production never hits the fallback.
package i18n

import (
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

const (
	CookieName       = "catena_lang"
	QueryParam       = "lang"
	DefaultLocaleEnv = "CATENA_DEFAULT_LANG"
)

// SupportedLocales is the ordered set of locales the shell serves. EN is the
// fallback and must stay first.
var SupportedLocales = []string{"en", "fr"}

func isSupported(loc string) bool {
	for _, l := range SupportedLocales {
		if l == loc {
			return true
		}
	}
	return false
}

// Translations is an in-memory bilingual store: read once, look up by dotted
// key with EN fallback on miss.
type Translations struct {
	tables map[string]map[string]string // locale -> dotted key -> value
}

// Load reads {locale}.yml for every supported locale from the directory dir.
// Convenience wrapper over LoadFS for the bind-mount / override path.
func Load(dir string) (*Translations, error) {
	return LoadFS(os.DirFS(dir))
}

// LoadFS reads {locale}.yml for every supported locale from fsys (root) and
// flattens each into a dotted-key table. A missing file yields an empty table
// for that locale (matching the Python loader), not an error -- so an embedded
// FS and a partial bind-mount both degrade to the EN fallback rather than
// crashing the shell on boot.
func LoadFS(fsys fs.FS) (*Translations, error) {
	t := &Translations{tables: make(map[string]map[string]string)}
	for _, locale := range SupportedLocales {
		name := locale + ".yml"
		raw, err := fs.ReadFile(fsys, name)
		if err != nil {
			if os.IsNotExist(err) {
				t.tables[locale] = map[string]string{}
				continue
			}
			return nil, fmt.Errorf("i18n: read %s: %w", name, err)
		}
		var nested map[string]any
		if err := yaml.Unmarshal(raw, &nested); err != nil {
			return nil, fmt.Errorf("i18n: parse %s: %w", name, err)
		}
		t.tables[locale] = flatten(nested, "")
	}
	return t, nil
}

// Get looks up key in locale. On a miss it falls back to EN (logged); on a
// miss in all locales it returns key. args interpolate {name} placeholders.
func (t *Translations) Get(key, locale string, args map[string]string) string {
	value, ok := t.tables[locale][key]
	if !ok && locale != "en" {
		if v, okEn := t.tables["en"][key]; okEn {
			value, ok = v, true
			fmt.Fprintf(os.Stderr, "i18n: missing translation for %q in locale %q; falling back to en\n", key, locale)
		}
	}
	if !ok {
		fmt.Fprintf(os.Stderr, "i18n: missing translation key %q in ALL locales\n", key)
		return key
	}
	if len(args) > 0 {
		return interpolate(value, args)
	}
	return value
}

// Keys returns the dotted keys present for locale (unordered).
func (t *Translations) Keys(locale string) []string {
	out := make([]string, 0, len(t.tables[locale]))
	for k := range t.tables[locale] {
		out = append(out, k)
	}
	return out
}

// MissingFromTarget returns the EN keys absent in target, sorted. The parity
// test asserts this is empty before shipping.
func (t *Translations) MissingFromTarget(target string) []string {
	var missing []string
	for k := range t.tables["en"] {
		if _, ok := t.tables[target][k]; !ok {
			missing = append(missing, k)
		}
	}
	sort.Strings(missing)
	return missing
}

// interpolate replaces {name} with args[name]; an unknown placeholder is left
// verbatim (the Python format() would raise, but leaving it visible is the
// safer UI behaviour and matches the "missing string stays visible" stance).
func interpolate(s string, args map[string]string) string {
	for k, v := range args {
		s = strings.ReplaceAll(s, "{"+k+"}", v)
	}
	return s
}

// flatten collapses a nested map into single-level dotted keys, stringifying
// leaf values (matching the Python _flatten + str()).
func flatten(d map[string]any, prefix string) map[string]string {
	out := make(map[string]string)
	for k, v := range d {
		path := k
		if prefix != "" {
			path = prefix + "." + k
		}
		switch child := v.(type) {
		case map[string]any:
			for ck, cv := range flatten(child, path) {
				out[ck] = cv
			}
		default:
			out[path] = stringify(v)
		}
	}
	return out
}

func stringify(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprintf("%v", v)
}

// ResolveLocale picks the request locale by precedence: ?lang query, then the
// catena_lang cookie, then the highest Accept-Language family match, then
// CATENA_DEFAULT_LANG, then "en".
func ResolveLocale(r *http.Request) string {
	if qp := r.URL.Query().Get(QueryParam); isSupported(qp) {
		return qp
	}
	if c, err := r.Cookie(CookieName); err == nil && isSupported(c.Value) {
		return c.Value
	}
	accept := r.Header.Get("Accept-Language")
	for _, chunk := range strings.Split(accept, ",") {
		tag := strings.ToLower(strings.TrimSpace(strings.SplitN(chunk, ";", 2)[0]))
		if tag == "" {
			continue
		}
		family := strings.SplitN(tag, "-", 2)[0]
		if isSupported(family) {
			return family
		}
	}
	if env := strings.ToLower(strings.TrimSpace(os.Getenv(DefaultLocaleEnv))); isSupported(env) {
		return env
	}
	return "en"
}
