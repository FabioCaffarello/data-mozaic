// controlled-source provides deterministic local acceptance fixtures only.
// It has no dependency on, or route to, any external data source.
package main

import (
	"crypto/tls"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

func main() {
	if len(os.Args) == 3 && os.Args[1] == "-healthcheck" {
		response, err := http.Get(os.Args[2])
		if err != nil {
			os.Exit(1)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusNoContent {
			os.Exit(1)
		}
		return
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	mux.HandleFunc("/v1/records", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("limit") != "2" {
			http.Error(w, "query denied", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"records":[{"id":"controlled-1","label":"alpha"},{"id":"controlled-2","label":"beta"}]}`)
	})
	mux.HandleFunc("/v1/items", func(w http.ResponseWriter, r *http.Request) {
		if values, present := r.URL.Query()["limit"]; present {
			if len(values) != 1 {
				http.Error(w, "query denied", http.StatusBadRequest)
				return
			}
			limit, err := strconv.Atoi(values[0])
			if err != nil || limit < 1 || limit > 3 {
				http.Error(w, "query denied", http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			if os.Getenv("CONTROLLED_SOURCE_MALFORMED") == "1" {
				// A valid authorized request whose source response is malformed
				// exercises post-admission acquisition failure without publication.
				_, _ = fmt.Fprint(w, `{"items":[`)
				return
			}
			_, _ = fmt.Fprintf(w, `{"items":[{"id":"parameter-%d","quantity":%d,"unit_price":%d.25,"enabled":true}]}`, limit, limit, limit)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"items":[{"id":"controlled-1","quantity":2,"unit_price":1.25,"enabled":true},{"id":"controlled-2","quantity":5,"unit_price":3.5,"enabled":false}]}`)
	})
	mux.HandleFunc("/v1/page/1", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"records":[{"id":"page-1"}],"next":"/v1/page/2"}`)
	})
	mux.HandleFunc("/v1/page/2", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"records":[{"id":"page-2"}],"next":null}`)
	})
	mux.HandleFunc("/v1/html", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = fmt.Fprint(w, `<!doctype html><html><body>
<article class="product" data-id="html-1"><h2 class="title">Controlled HTML</h2><span class="price">12.5</span></article>
</body></html>
`)
	})
	mux.HandleFunc("/v1/pages/1", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = fmt.Fprint(w, `<html><body><article class="item" data-id="page-1"><span class="name">First page</span></article><a class="next" href="/v1/pages/2">Next</a></body></html>`)
	})
	mux.HandleFunc("/v1/pages/2", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = fmt.Fprint(w, `<html><body><article class="item" data-id="page-2"><span class="name">Second page</span></article></body></html>`)
	})
	mux.HandleFunc("/v1/window/1", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = fmt.Fprint(w, `<html><body><article class="item" data-id="page-1"><span class="name">First page</span></article><a class="next" href="/v1/window/2">Next</a></body></html>`)
	})
	mux.HandleFunc("/v1/window/2", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = fmt.Fprint(w, `<html><body><article class="item" data-id="page-2"><span class="name">Second page</span></article><a class="next" href="/v1/window/3">Next</a></body></html>`)
	})
	mux.HandleFunc("/v1/redirect", func(w http.ResponseWriter, r *http.Request) {
		// Keep the controlled first hop longer than the configured 100 ms
		// request-rate interval so the redirect target is admitted by policy.
		time.Sleep(150 * time.Millisecond)
		http.Redirect(w, r, "/v1/items", http.StatusTemporaryRedirect)
	})
	mux.HandleFunc("/v1/redirect-chain/", func(w http.ResponseWriter, r *http.Request) {
		// Model a deterministic source latency so per-hop request-rate limits
		// do not mask the separate redirect-count limit under test.
		time.Sleep(150 * time.Millisecond)
		current := strings.TrimPrefix(r.URL.Path, "/v1/redirect-chain/")
		n, err := strconv.Atoi(current)
		if err != nil || n < 0 {
			http.NotFound(w, r)
			return
		}
		http.Redirect(w, r, fmt.Sprintf("/v1/redirect-chain/%d", n+1), http.StatusTemporaryRedirect)
	})
	mux.HandleFunc("/v1/oversized", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/octet-stream")
		_, _ = w.Write(make([]byte, 2_100_000))
	})
	mux.HandleFunc("/v1/slow", func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(20 * time.Second)
		_, _ = fmt.Fprint(w, `{"slow":true}`)
	})
	mux.HandleFunc("/v1/malformed", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"records":[`)
	})
	mux.HandleFunc("/v1/invalid-output", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"items":[{"id":"invalid-1","quantity":"not-an-integer","unit_price":1.25,"enabled":true}]}`)
	})
	mux.HandleFunc("/v1/interrupted-body", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		flusher, flushes := w.(http.Flusher)
		hijacker, hijacks := w.(http.Hijacker)
		if !flushes || !hijacks {
			return
		}
		_, _ = fmt.Fprint(w, `{"items":[`)
		flusher.Flush()
		connection, _, err := hijacker.Hijack()
		if err == nil {
			_ = connection.Close()
		}
	})
	mux.HandleFunc("/v1/status/", func(w http.ResponseWriter, r *http.Request) {
		status, err := strconv.Atoi(r.URL.Path[len("/v1/status/"):])
		if err != nil {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(status)
	})
	server := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, MaxHeaderBytes: 8 << 10}
	tlsServer := &http.Server{Addr: ":443", Handler: mux, ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, MaxHeaderBytes: 8 << 10, TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12}}
	go func() {
		if err := tlsServer.ListenAndServeTLS("/run/controlled/controlled-source.crt", "/run/controlled/controlled-source.key"); err != nil && err != http.ErrServerClosed {
			panic(err)
		}
	}()
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		panic(err)
	}
}
