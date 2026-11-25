package httpapi

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"unicode/utf8"
)

// decodeStrict rejects duplicate properties, nulls, invalid UTF-8, lone UTF-16
// surrogates, trailing values and unknown struct fields at the HTTP boundary.
func decodeStrict(raw []byte, target any) error {
	if !utf8.Valid(raw) || !validSurrogateEscapes(raw) {
		return errors.New("request JSON contains invalid Unicode")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := walkValue(decoder); err != nil {
		return err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("request JSON contains trailing content")
		}
		return err
	}
	strict := json.NewDecoder(bytes.NewReader(raw))
	strict.DisallowUnknownFields()
	if err := strict.Decode(target); err != nil {
		return err
	}
	if err := strict.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("request JSON contains trailing content")
	}
	return nil
}

func walkValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	switch value := token.(type) {
	case nil:
		return errors.New("null values are not accepted; omit absent fields")
	case json.Delim:
		switch value {
		case '{':
			seen := map[string]struct{}{}
			for decoder.More() {
				keyToken, err := decoder.Token()
				if err != nil {
					return err
				}
				key, ok := keyToken.(string)
				if !ok {
					return errors.New("JSON object property name is not a string")
				}
				if _, exists := seen[key]; exists {
					return errors.New("duplicate JSON property")
				}
				seen[key] = struct{}{}
				if err := walkValue(decoder); err != nil {
					return err
				}
			}
			end, err := decoder.Token()
			if err != nil || end != json.Delim('}') {
				return errors.New("malformed JSON object")
			}
		case '[':
			for decoder.More() {
				if err := walkValue(decoder); err != nil {
					return err
				}
			}
			end, err := decoder.Token()
			if err != nil || end != json.Delim(']') {
				return errors.New("malformed JSON array")
			}
		default:
			return errors.New("unexpected JSON delimiter")
		}
	}
	return nil
}

func validSurrogateEscapes(raw []byte) bool {
	for i := 0; i < len(raw); i++ {
		if raw[i] != '"' {
			continue
		}
		for i++; i < len(raw); i++ {
			if raw[i] == '"' {
				break
			}
			if raw[i] != '\\' {
				continue
			}
			i++
			if i >= len(raw) {
				return false
			}
			if raw[i] != 'u' {
				continue
			}
			code, ok := parseHex4(raw, i+1)
			if !ok {
				return false
			}
			i += 4
			switch {
			case code >= 0xD800 && code <= 0xDBFF:
				if i+6 >= len(raw) || raw[i+1] != '\\' || raw[i+2] != 'u' {
					return false
				}
				low, ok := parseHex4(raw, i+3)
				if !ok || low < 0xDC00 || low > 0xDFFF {
					return false
				}
				i += 6
			case code >= 0xDC00 && code <= 0xDFFF:
				return false
			}
		}
	}
	return true
}

func parseHex4(raw []byte, start int) (uint16, bool) {
	if start+4 > len(raw) {
		return 0, false
	}
	var value uint16
	for _, c := range raw[start : start+4] {
		value <<= 4
		switch {
		case c >= '0' && c <= '9':
			value |= uint16(c - '0')
		case c >= 'a' && c <= 'f':
			value |= uint16(c - 'a' + 10)
		case c >= 'A' && c <= 'F':
			value |= uint16(c - 'A' + 10)
		default:
			return 0, false
		}
	}
	return value, true
}
