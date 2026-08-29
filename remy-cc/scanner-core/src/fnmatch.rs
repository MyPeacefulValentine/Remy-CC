//! Python `fnmatch.fnmatch` replication.
//!
//! `fnmatch.fnmatch` applies `os.path.normcase` to both arguments before
//! matching, so matching is case-insensitive on Windows and case-sensitive
//! elsewhere. normcase's `/` → `\` translation is a bijection applied to
//! both sides, so it never changes the match outcome for the `/`-separated
//! inputs the scanner produces and is not replicated.
//!
//! Wildcards: `*` matches any run (including separators), `?` any single
//! character, `[seq]` / `[!seq]` character classes with `-` ranges. An
//! unterminated `[` matches itself literally.

pub fn fnmatch(name: &str, pattern: &str) -> bool {
    if cfg!(windows) {
        fnmatchcase(&name.to_lowercase(), &pattern.to_lowercase())
    } else {
        fnmatchcase(name, pattern)
    }
}

#[derive(Debug, Clone, PartialEq)]
enum Tok {
    Star,
    Any,
    Lit(char),
    Class {
        negated: bool,
        ranges: Vec<(char, char)>,
    },
}

fn tokenize(pattern: &str) -> Vec<Tok> {
    let chars: Vec<char> = pattern.chars().collect();
    let mut toks = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        match chars[i] {
            '*' => {
                if toks.last() != Some(&Tok::Star) {
                    toks.push(Tok::Star);
                }
                i += 1;
            }
            '?' => {
                toks.push(Tok::Any);
                i += 1;
            }
            '[' => match parse_class(&chars, i) {
                Some((tok, next)) => {
                    toks.push(tok);
                    i = next;
                }
                None => {
                    toks.push(Tok::Lit('['));
                    i += 1;
                }
            },
            c => {
                toks.push(Tok::Lit(c));
                i += 1;
            }
        }
    }
    toks
}

fn parse_class(chars: &[char], open: usize) -> Option<(Tok, usize)> {
    let mut j = open + 1;
    let negated = chars.get(j) == Some(&'!');
    if negated {
        j += 1;
    }
    let body_start = j;
    if chars.get(j) == Some(&']') {
        j += 1;
    }
    while j < chars.len() && chars[j] != ']' {
        j += 1;
    }
    if j >= chars.len() {
        return None;
    }
    let body = &chars[body_start..j];
    let mut ranges = Vec::new();
    let mut k = 0;
    while k < body.len() {
        if k + 2 < body.len() && body[k + 1] == '-' {
            ranges.push((body[k], body[k + 2]));
            k += 3;
        } else if k + 2 == body.len() && body[k + 1] == '-' {
            ranges.push((body[k], body[k]));
            ranges.push(('-', '-'));
            k += 2;
        } else {
            ranges.push((body[k], body[k]));
            k += 1;
        }
    }
    Some((Tok::Class { negated, ranges }, j + 1))
}

fn class_matches(negated: bool, ranges: &[(char, char)], c: char) -> bool {
    let hit = ranges.iter().any(|(lo, hi)| *lo <= c && c <= *hi);
    hit != negated
}

fn fnmatchcase(name: &str, pattern: &str) -> bool {
    let toks = tokenize(pattern);
    let chars: Vec<char> = name.chars().collect();

    let mut t = 0;
    let mut n = 0;
    let mut star_t: Option<usize> = None;
    let mut star_n = 0;

    while n < chars.len() {
        let matched = match toks.get(t) {
            Some(Tok::Star) => {
                star_t = Some(t);
                star_n = n;
                t += 1;
                continue;
            }
            Some(Tok::Any) => true,
            Some(Tok::Lit(c)) => *c == chars[n],
            Some(Tok::Class { negated, ranges }) => class_matches(*negated, ranges, chars[n]),
            None => false,
        };
        if matched {
            t += 1;
            n += 1;
        } else if let Some(st) = star_t {
            star_n += 1;
            t = st + 1;
            n = star_n;
        } else {
            return false;
        }
    }
    while toks.get(t) == Some(&Tok::Star) {
        t += 1;
    }
    t == toks.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn basic_wildcards() {
        assert!(fnmatchcase("foo.c", "*.c"));
        assert!(!fnmatchcase("foo.cc", "*.c"));
        assert!(fnmatchcase("a/b/c.c", "*.c"));
        assert!(fnmatchcase("abc", "a?c"));
        assert!(fnmatchcase("node_modules", "node_modules"));
        assert!(fnmatchcase("x", "*"));
        assert!(fnmatchcase("", "*"));
        assert!(!fnmatchcase("", "?"));
    }

    #[test]
    fn character_classes() {
        assert!(fnmatchcase("a1", "a[0-9]"));
        assert!(!fnmatchcase("ax", "a[0-9]"));
        assert!(fnmatchcase("ax", "a[!0-9]"));
        assert!(fnmatchcase("a]", "a[]]"));
        assert!(fnmatchcase("a[", "a["));
        assert!(fnmatchcase("a-b", "a[-]b"));
    }

    #[cfg(windows)]
    #[test]
    fn windows_matching_is_case_insensitive() {
        // Probe F1 (2026-08-16, win32).
        assert!(fnmatch("FOO.C", "*.c"));
        assert!(fnmatch("Build", "build"));
    }

    #[cfg(not(windows))]
    #[test]
    fn posix_matching_is_case_sensitive() {
        assert!(!fnmatch("FOO.C", "*.c"));
        assert!(!fnmatch("Build", "build"));
    }
}
