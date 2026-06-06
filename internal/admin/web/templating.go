// Package web is the catena-admin shell's HTTP surface: templating,
// per-request middleware, and the CE routes. Ported from the Python
// catena_admin (FastAPI + Jinja) to net/http + html/template.
package web

import (
	"embed"
	"html/template"
	"io/fs"
	"log"
	"net/http"
	"path"
	"strings"

	"github.com/catenahq/catena-ce/internal/admin/auth"
	"github.com/catenahq/catena-ce/internal/admin/i18n"
)

//go:embed templates/layout.tmpl templates/nav.tmpl templates/pages/*.tmpl templates/fragments/*.tmpl
var templateFS embed.FS

//go:embed static
var staticFS embed.FS

//go:embed translations
var translationsFS embed.FS

// Templates renders the shell pages. Each page is the shared layout + nav
// cloned and combined with that page's "content" (and optional "title")
// block, so pages compose like Jinja's {% extends %} without a shared
// mutable set.
type Templates struct {
	tr        *i18n.Translations
	pages     map[string]*template.Template
	fragments map[string]*template.Template
	globals   map[string]any
}

// NewTemplates parses the embedded layout + nav + every page template.
func NewTemplates(tr *i18n.Translations, globals map[string]any) (*Templates, error) {
	fm := template.FuncMap{
		"upper":     strings.ToUpper,
		"hasPrefix": strings.HasPrefix,
		// safeHTML emits a trusted, static fragment (action/artifact icon
		// entities from the catalog + recovery package) without escaping.
		// Only ever called on constants, never on user input.
		"safeHTML": func(s string) template.HTML { return template.HTML(s) },
	}
	base := template.New("base").Funcs(fm)
	for _, f := range []string{"templates/layout.tmpl", "templates/nav.tmpl"} {
		b, err := templateFS.ReadFile(f)
		if err != nil {
			return nil, err
		}
		if _, err := base.Parse(string(b)); err != nil {
			return nil, err
		}
	}
	pageFiles, err := fs.Glob(templateFS, "templates/pages/*.tmpl")
	if err != nil {
		return nil, err
	}
	pages := make(map[string]*template.Template, len(pageFiles))
	for _, pf := range pageFiles {
		name := strings.TrimSuffix(path.Base(pf), ".tmpl")
		clone, err := base.Clone()
		if err != nil {
			return nil, err
		}
		b, err := templateFS.ReadFile(pf)
		if err != nil {
			return nil, err
		}
		if _, err := clone.Parse(string(b)); err != nil {
			return nil, err
		}
		pages[name] = clone
	}
	// Fragments are htmx swap targets rendered without the base layout (the
	// run-panel swapped into the Actions output div). Each is parsed standalone
	// against the same FuncMap; they define their own top-level template name.
	fragFiles, err := fs.Glob(templateFS, "templates/fragments/*.tmpl")
	if err != nil {
		return nil, err
	}
	fragments := make(map[string]*template.Template, len(fragFiles))
	for _, ff := range fragFiles {
		name := strings.TrimSuffix(path.Base(ff), ".tmpl")
		t := template.New(name).Funcs(fm)
		b, err := templateFS.ReadFile(ff)
		if err != nil {
			return nil, err
		}
		if _, err := t.Parse(string(b)); err != nil {
			return nil, err
		}
		fragments[name] = t
	}
	return &Templates{tr: tr, pages: pages, fragments: fragments, globals: globals}, nil
}

// renderData is the per-render context templates consume. T/Tf bind the
// request locale so templates call {{ .T "key" }} like the Python {{ _("key") }}.
type renderData struct {
	Locale   string
	Theme    string
	Identity auth.Identity
	IsAdmin  bool
	Globals  map[string]any
	Path     string
	Data     any

	tr *i18n.Translations
}

// T translates key in the request locale.
func (d *renderData) T(key string) string { return d.tr.Get(key, d.Locale, nil) }

// Tf translates key with interpolation; pass alternating name/value pairs:
// Tf("greeting", "name", "Marc").
func (d *renderData) Tf(key string, kv ...string) string {
	args := make(map[string]string, len(kv)/2)
	for i := 0; i+1 < len(kv); i += 2 {
		args[kv[i]] = kv[i+1]
	}
	return d.tr.Get(key, d.Locale, args)
}

// Render writes page (a "content" template name) wrapped in the base layout,
// pulling locale/theme/identity from the request context (set by RequestState).
func (t *Templates) Render(w http.ResponseWriter, r *http.Request, page string, status int, data any) {
	tmpl, ok := t.pages[page]
	if !ok {
		http.Error(w, "template not found: "+page, http.StatusInternalServerError)
		return
	}
	id := identityFrom(r)
	rd := &renderData{
		Locale:   localeFrom(r),
		Theme:    themeFrom(r),
		Identity: id,
		IsAdmin:  id.IsAdmin(),
		Globals:  t.globals,
		Path:     r.URL.Path,
		Data:     data,
		tr:       t.tr,
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	if err := tmpl.ExecuteTemplate(w, "base", rd); err != nil {
		// The status + some bytes may already be on the wire; logging is the
		// only safe recourse.
		log.Printf("web: render %q: %v", page, err)
	}
}

// RenderFragment writes a bare fragment (no base layout) -- the htmx swap
// targets. Same renderData shape as Render so fragments call {{ .T }} and read
// {{ .Data }}, but without the page chrome.
func (t *Templates) RenderFragment(w http.ResponseWriter, r *http.Request, name string, status int, data any) {
	tmpl, ok := t.fragments[name]
	if !ok {
		http.Error(w, "fragment not found: "+name, http.StatusInternalServerError)
		return
	}
	id := identityFrom(r)
	rd := &renderData{
		Locale:   localeFrom(r),
		Theme:    themeFrom(r),
		Identity: id,
		IsAdmin:  id.IsAdmin(),
		Globals:  t.globals,
		Path:     r.URL.Path,
		Data:     data,
		tr:       t.tr,
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	if err := tmpl.ExecuteTemplate(w, name, rd); err != nil {
		log.Printf("web: render fragment %q: %v", name, err)
	}
}
