package gateway

import (
	"encoding/json"
	"errors"
	"net"
	"net/netip"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

var safeScope = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/?=&%+_-]*$`)
var encodedSeparator = regexp.MustCompile(`(?i)%(2f|5c)`)

type Policy struct {
	ContractVersion       string              `json:"contract_version"`
	PolicyID              string              `json:"policy_id"`
	PolicyVersion         string              `json:"policy_version"`
	SourceID              string              `json:"source_id"`
	SourceRevisionID      string              `json:"source_revision_id"`
	Status                string              `json:"status"`
	Origins               []string            `json:"origins"`
	Methods               []string            `json:"methods"`
	PathPrefixes          []string            `json:"path_prefixes"`
	QueryParameterValues  map[string][]string `json:"query_parameter_values"`
	DNSPolicy             string              `json:"dns_policy"`
	TestDestinationCIDRs  []string            `json:"test_destination_cidrs,omitempty"`
	RedirectPolicy        string              `json:"redirect_policy"`
	MaxRequests           int                 `json:"max_requests"`
	MaxConcurrency        int                 `json:"max_concurrency"`
	RequestsPerSecond     int                 `json:"requests_per_second"`
	MaxResponseBytes      int64               `json:"max_response_bytes"`
	MaxTotalResponseBytes int64               `json:"max_total_response_bytes"`
	DeadlineSeconds       int                 `json:"deadline_seconds"`
	RequireTLS            bool                `json:"require_tls"`
	CredentialRefs        []string            `json:"credential_refs"`
	EvidenceRetentionRef  string              `json:"evidence_retention_policy_ref"`
	PolicyDigest          string              `json:"policy_digest"`
}

type PolicySet struct {
	Policies []Policy `json:"policies"`
}

func (p Policy) Validate() error {
	if p.ContractVersion != "acquisition-policy/v1" || p.Status != "eligible" || p.PolicyID == "" || p.SourceID == "" || p.SourceRevisionID == "" || !digestValue(p.PolicyDigest) {
		return errors.New("policy_invalid")
	}
	if len(p.Origins) == 0 || len(p.PathPrefixes) == 0 || len(p.Methods) != 1 || p.Methods[0] != "GET" {
		return errors.New("policy_scope_invalid")
	}
	if p.MaxRequests < 1 || p.MaxRequests > 50 || p.MaxConcurrency < 1 || p.MaxConcurrency > 4 || p.RequestsPerSecond < 1 || p.RequestsPerSecond > 10 || p.MaxResponseBytes < 1 || p.MaxTotalResponseBytes < p.MaxResponseBytes || p.DeadlineSeconds < 1 || p.DeadlineSeconds > 300 {
		return errors.New("policy_budget_invalid")
	}
	if p.DNSPolicy == "test-fixture-only" {
		if p.RequireTLS {
			return errors.New("test_policy_tls_invalid")
		}
		if len(p.TestDestinationCIDRs) > 8 {
			return errors.New("test_destination_scope_invalid")
		}
		for _, raw := range p.TestDestinationCIDRs {
			prefix, err := netip.ParsePrefix(raw)
			if err != nil || prefix.Bits() != prefix.Addr().BitLen() || !prefix.Addr().IsPrivate() {
				return errors.New("test_destination_scope_invalid")
			}
		}
	} else if p.DNSPolicy != "public-addresses-only" || !p.RequireTLS {
		return errors.New("policy_dns_invalid")
	} else if len(p.TestDestinationCIDRs) != 0 {
		return errors.New("test_destination_scope_invalid")
	}
	if p.RedirectPolicy != "deny" && p.RedirectPolicy != "revalidate-every-hop" {
		return errors.New("policy_redirect_invalid")
	}
	for _, origin := range p.Origins {
		if _, _, err := parseOrigin(origin, p.DNSPolicy == "test-fixture-only"); err != nil {
			return errors.New("policy_origin_invalid")
		}
		if strings.Contains(origin, "controlled-source") && len(p.TestDestinationCIDRs) == 0 {
			return errors.New("test_destination_scope_invalid")
		}
	}
	for _, prefix := range p.PathPrefixes {
		if _, err := normalizePath(prefix); err != nil {
			return errors.New("policy_path_invalid")
		}
	}
	return nil
}

func (p Policy) VerifyDigest() error {
	body, err := json.Marshal(p)
	if err != nil {
		return errors.New("policy_digest_invalid")
	}
	var fields map[string]any
	if err = json.Unmarshal(body, &fields); err != nil {
		return errors.New("policy_digest_invalid")
	}
	delete(fields, "policy_digest")
	canonical, err := json.Marshal(fields)
	if err != nil || hashDigest(canonical) != p.PolicyDigest {
		return errors.New("policy_digest_invalid")
	}
	return nil
}

func parseOrigin(raw string, allowLocalHTTP bool) (string, int, error) {
	u, err := url.Parse(raw)
	if err != nil || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || u.Host == "" {
		return "", 0, errors.New("origin_invalid")
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme != "https" && !(allowLocalHTTP && scheme == "http") {
		return "", 0, errors.New("scheme_denied")
	}
	host := strings.ToLower(u.Hostname())
	if host == "" || strings.HasSuffix(host, ".") || strings.ContainsAny(host, "%\\") {
		return "", 0, errors.New("host_invalid")
	}
	port := 443
	if scheme == "http" {
		port = 80
	}
	if u.Port() != "" {
		port, err = strconv.Atoi(u.Port())
		if err != nil || port < 1 || port > 65535 {
			return "", 0, errors.New("port_invalid")
		}
	}
	if scheme == "http" {
		ip := net.ParseIP(host)
		if (ip == nil && host != "localhost" && host != "controlled-source") || (ip != nil && !ip.IsLoopback()) {
			return "", 0, errors.New("test_destination_denied")
		}
	}
	defaultPort := 443
	if scheme == "http" {
		defaultPort = 80
	}
	canonical := scheme + "://" + host
	if strings.Contains(host, ":") {
		canonical = scheme + "://[" + host + "]"
	}
	if port != defaultPort {
		canonical += ":" + strconv.Itoa(port)
	}
	return canonical, port, nil
}

func normalizePath(raw string) (string, error) {
	if raw == "" {
		raw = "/"
	}
	if !strings.HasPrefix(raw, "/") || hasBadEscape(raw) || encodedSeparator.MatchString(raw) || strings.Contains(raw, "\\") {
		return "", errors.New("path_invalid")
	}
	path := raw
	for i := 0; i < 8; i++ {
		decoded, err := url.PathUnescape(path)
		if err != nil {
			return "", errors.New("path_invalid")
		}
		if decoded == path {
			break
		}
		path = decoded
		if encodedSeparator.MatchString(path) || strings.Contains(path, "\\") {
			return "", errors.New("path_separator_denied")
		}
		if i == 7 {
			return "", errors.New("path_encoding_too_deep")
		}
	}
	if strings.Contains(path, "%") {
		return "", errors.New("path_ambiguous")
	}
	for _, part := range strings.Split(path, "/") {
		if part == "." || part == ".." {
			return "", errors.New("path_traversal_denied")
		}
	}
	return path, nil
}

func hasBadEscape(value string) bool {
	for i := 0; i < len(value); i++ {
		if value[i] != '%' {
			continue
		}
		if i+2 >= len(value) || !isHex(value[i+1]) || !isHex(value[i+2]) {
			return true
		}
		i += 2
	}
	return false
}

func isHex(value byte) bool {
	return value >= '0' && value <= '9' || value >= 'a' && value <= 'f' || value >= 'A' && value <= 'F'
}

func (p Policy) AuthorizeURL(raw string) (*url.URL, error) {
	if len(raw) > 4096 || !safeScope.MatchString(raw) || strings.ContainsAny(raw, "\r\n\\") || hasBadEscape(raw) {
		return nil, errors.New("url_invalid")
	}
	u, err := url.Parse(raw)
	if err != nil || u.User != nil || u.Fragment != "" || u.Host == "" {
		return nil, errors.New("url_invalid")
	}
	origin, _, err := parseOrigin(u.Scheme+"://"+u.Host, p.DNSPolicy == "test-fixture-only")
	if err != nil {
		return nil, err
	}
	allowedOrigin := false
	for _, configured := range p.Origins {
		candidate, _, originErr := parseOrigin(configured, p.DNSPolicy == "test-fixture-only")
		if originErr == nil && origin == candidate {
			allowedOrigin = true
			break
		}
	}
	if !allowedOrigin {
		return nil, errors.New("origin_denied")
	}
	path, err := normalizePath(u.EscapedPath())
	if err != nil {
		return nil, err
	}
	allowedPath := false
	for _, prefix := range p.PathPrefixes {
		normalized, pathErr := normalizePath(prefix)
		if pathErr == nil && pathWithin(path, normalized) {
			allowedPath = true
			break
		}
	}
	if !allowedPath {
		return nil, errors.New("path_denied")
	}
	query, err := url.ParseQuery(u.RawQuery)
	if err != nil {
		return nil, errors.New("query_invalid")
	}
	for name, values := range query {
		allowed, exists := p.QueryParameterValues[name]
		if !exists || len(values) != 1 || strings.Contains(values[0], "%") || !contains(allowed, values[0]) {
			return nil, errors.New("query_denied")
		}
	}
	return u, nil
}

func pathWithin(path, prefix string) bool {
	trimmed := strings.TrimSuffix(prefix, "/")
	return prefix == "/" || path == trimmed || strings.HasPrefix(path, trimmed+"/")
}

func contains(values []string, value string) bool {
	for _, candidate := range values {
		if candidate == value {
			return true
		}
	}
	return false
}
