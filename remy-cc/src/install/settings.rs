//! Claude settings.json ownership: template rendering, claim-tracked merge,
//! claim verification and removal.
//!
//! Behavioral port of the retired v3 installer's settings module (its
//! pytest surface was the porting reference); error
//! message texts are kept verbatim. Two deliberate divergences, both turning
//! latent Python crashes into controlled errors: a fresh install against a
//! non-object `hooks` value reports the metadata error instead of an
//! AttributeError, and claim removal tolerates a missing `permissions`
//! section instead of a KeyError. One addition for the R4.4 rename: the old
//! `remy-daemon hook …` default commands are cleared as legacy exactly like
//! the retired python-script defaults.

use std::path::Path;

use serde_json::{Map, Value};

use super::InstallError;

pub(crate) const ENRICH_PLACEHOLDER: &str = "__REMY_ENRICH_COMMAND__";
pub(crate) const DIRTY_PLACEHOLDER: &str = "__REMY_DIRTY_COMMAND__";

/// Retired python-arm hook registrations, identified by script name so
/// upgrades still remove them from settings.json.
const TARGET_HOOKS: &[(&str, &str, &str)] = &[
    ("PreToolUse", "Read|Glob|Grep", "logic_enrichment_hook.py"),
    ("PostToolUse", "Edit|Write", "logic_dirty_tracker.py"),
];

/// The pre-rename managed executable name whose default commands are now
/// cleared as legacy.
const LEGACY_DAEMON_STEM: &str = "remy-daemon";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct HookCommands {
    pub(crate) enrich: String,
    pub(crate) dirty: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub(crate) struct SettingsClaim {
    pub(crate) hooks: Vec<ClaimedHook>,
    pub(crate) permissions: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ClaimedHook {
    pub(crate) event: String,
    pub(crate) matcher: String,
    pub(crate) command: String,
}

impl SettingsClaim {
    pub(crate) fn to_value(&self) -> Value {
        Value::Object(Map::from_iter([
            (
                "hooks".to_string(),
                Value::Array(
                    self.hooks
                        .iter()
                        .map(|hook| {
                            Value::Object(Map::from_iter([
                                ("event".to_string(), Value::String(hook.event.clone())),
                                ("matcher".to_string(), Value::String(hook.matcher.clone())),
                                ("command".to_string(), Value::String(hook.command.clone())),
                            ]))
                        })
                        .collect(),
                ),
            ),
            (
                "permissions".to_string(),
                Value::Array(
                    self.permissions
                        .iter()
                        .cloned()
                        .map(Value::String)
                        .collect(),
                ),
            ),
        ]))
    }

    pub(crate) fn from_value(value: &Value) -> Result<Self, InstallError> {
        let object = value
            .as_object()
            .ok_or_else(|| InstallError::metadata("manifest settings_claim must be an object"))?;
        let hooks = object
            .get("hooks")
            .and_then(Value::as_array)
            .ok_or_else(|| InstallError::metadata("invalid manifest settings_claim"))?;
        let permissions = object
            .get("permissions")
            .and_then(Value::as_array)
            .ok_or_else(|| InstallError::metadata("invalid manifest settings_claim"))?;
        let mut claim = SettingsClaim::default();
        for hook in hooks {
            let entry = hook
                .as_object()
                .ok_or_else(|| InstallError::metadata("invalid manifest Hook claim"))?;
            let field = |key: &str| -> Result<String, InstallError> {
                entry
                    .get(key)
                    .and_then(Value::as_str)
                    .map(str::to_string)
                    .ok_or_else(|| InstallError::metadata("invalid manifest Hook claim"))
            };
            claim.hooks.push(ClaimedHook {
                event: field("event")?,
                matcher: field("matcher")?,
                command: field("command")?,
            });
        }
        for permission in permissions {
            claim.permissions.push(
                permission
                    .as_str()
                    .ok_or_else(|| InstallError::metadata("invalid manifest permission claim"))?
                    .to_string(),
            );
        }
        Ok(claim)
    }
}

pub(crate) fn quote_command_arg(value: &str) -> Result<String, InstallError> {
    if value.contains('"') || value.contains('\r') || value.contains('\n') {
        return Err(InstallError::runtime(
            "managed command path contains unsupported characters",
        ));
    }
    Ok(format!("\"{value}\""))
}

/// Managed Hook command strings for the deployed binary at
/// `<remy_root>/bin/remy-cc[.exe]` (extension by path shape, matching the
/// Python `_is_windows_path` rule so tests behave identically per platform).
pub(crate) fn hook_commands(remy_root: &Path) -> Result<HookCommands, InstallError> {
    let executable = remy_root.join("bin").join(managed_exe_name(remy_root));
    let prefix = quote_command_arg(&executable.to_string_lossy())?;
    Ok(HookCommands {
        enrich: format!("{prefix} hook enrich"),
        dirty: format!("{prefix} hook dirty"),
    })
}

pub(crate) fn managed_exe_name(remy_root: &Path) -> &'static str {
    if is_windows_path(remy_root) {
        "remy-cc.exe"
    } else {
        "remy-cc"
    }
}

fn legacy_exe_name(remy_root: &Path) -> &'static str {
    if is_windows_path(remy_root) {
        "remy-daemon.exe"
    } else {
        "remy-daemon"
    }
}

fn is_windows_path(path: &Path) -> bool {
    let text = path.to_string_lossy();
    text.starts_with("\\\\") || text.as_bytes().get(1) == Some(&b':')
}

pub(crate) fn merge_settings_document(
    existing: &Value,
    template: &Value,
    claude_root: &Path,
    remy_root: &Path,
    commands: &HookCommands,
    prior_claim: Option<&SettingsClaim>,
) -> Result<(Value, SettingsClaim), InstallError> {
    let mut result = existing.clone();
    if !result.is_object() {
        return Err(InstallError::metadata("settings.json must be an object"));
    }
    let rendered = render_template(template, claude_root, commands);
    if let Some(claim) = prior_claim {
        verify_settings_claim(&result, claim)?;
    }
    remove_prior_target_hooks(&mut result, claude_root, remy_root, prior_claim)?;

    let mut claim = SettingsClaim::default();
    let empty = Map::new();
    let rendered_hooks = match rendered.get("hooks") {
        Some(value) => value
            .as_object()
            .ok_or_else(|| InstallError::metadata("settings template hooks must be arrays"))?,
        None => &empty,
    };
    let result_object = result.as_object_mut().expect("checked above");
    let result_hooks = result_object
        .entry("hooks")
        .or_insert_with(|| Value::Object(Map::new()));
    let result_hooks = result_hooks
        .as_object_mut()
        .ok_or_else(|| InstallError::metadata("settings hooks must be an object"))?;
    for (event, entries) in rendered_hooks {
        let entries = entries
            .as_array()
            .ok_or_else(|| InstallError::metadata("settings template hooks must be arrays"))?;
        let destination = result_hooks
            .entry(event.clone())
            .or_insert_with(|| Value::Array(Vec::new()));
        let destination = destination
            .as_array_mut()
            .ok_or_else(|| InstallError::metadata("settings event hooks must be arrays"))?;
        for entry in entries {
            let entry = entry
                .as_object()
                .ok_or_else(|| InstallError::metadata("settings hook entry must be an object"))?;
            let matcher = string_view(entry.get("matcher"));
            if !destination
                .iter()
                .any(|item| item.is_object() && string_view(item.get("matcher")) == matcher)
            {
                destination.push(Value::Object(Map::from_iter([
                    ("matcher".to_string(), Value::String(matcher.clone())),
                    ("hooks".to_string(), Value::Array(Vec::new())),
                ])));
            }
            let target_entry = destination
                .iter_mut()
                .find(|item| {
                    item.is_object()
                        && string_view(item.as_object().unwrap().get("matcher")) == matcher
                })
                .expect("inserted above")
                .as_object_mut()
                .expect("object checked");
            let hooks = target_entry
                .entry("hooks")
                .or_insert_with(|| Value::Array(Vec::new()));
            let hooks = hooks
                .as_array_mut()
                .ok_or_else(|| InstallError::metadata("settings hooks list must be an array"))?;
            for hook in entry
                .get("hooks")
                .and_then(Value::as_array)
                .unwrap_or(&Vec::new())
            {
                let command = hook
                    .as_object()
                    .and_then(|h| h.get("command"))
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        InstallError::metadata("settings hook command must be a string")
                    })?;
                let command = command.trim().to_string();
                if !hooks.iter().any(|current| {
                    current
                        .as_object()
                        .and_then(|c| c.get("command"))
                        .and_then(Value::as_str)
                        .is_some_and(|c| c.trim() == command)
                }) {
                    hooks.push(hook.clone());
                }
                claim.hooks.push(ClaimedHook {
                    event: event.clone(),
                    matcher: matcher.clone(),
                    command,
                });
            }
        }
    }

    let template_permissions = rendered
        .get("permissions")
        .and_then(|p| p.get("allow"))
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let template_permissions = template_permissions
        .as_array()
        .filter(|items| items.iter().all(Value::is_string))
        .ok_or_else(|| InstallError::metadata("settings template permissions must be strings"))?;
    let result_object = result.as_object_mut().expect("checked above");
    let permissions_section = result_object
        .entry("permissions")
        .or_insert_with(|| Value::Object(Map::new()));
    let permissions_section = permissions_section
        .as_object_mut()
        .ok_or_else(|| InstallError::metadata("settings permissions must be an object"))?;
    let allow = permissions_section
        .entry("allow")
        .or_insert_with(|| Value::Array(Vec::new()));
    let allow = allow
        .as_array_mut()
        .ok_or_else(|| InstallError::metadata("settings permissions.allow must be an array"))?;
    let prior_permissions: Vec<&str> = prior_claim
        .map(|claim| claim.permissions.iter().map(String::as_str).collect())
        .unwrap_or_default();
    for permission in template_permissions {
        let text = permission.as_str().expect("filtered above");
        if !allow.iter().any(|item| item.as_str() == Some(text)) {
            allow.push(permission.clone());
            claim.permissions.push(text.to_string());
        } else if prior_permissions.contains(&text) {
            claim.permissions.push(text.to_string());
        }
    }

    let template_env = rendered
        .get("env")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let template_env = template_env
        .as_object()
        .ok_or_else(|| InstallError::metadata("settings env must be an object"))?;
    let result_object = result.as_object_mut().expect("checked above");
    let env = result_object
        .entry("env")
        .or_insert_with(|| Value::Object(Map::new()));
    let env = env
        .as_object_mut()
        .ok_or_else(|| InstallError::metadata("settings env must be an object"))?;
    for (key, value) in template_env {
        env.entry(key.clone()).or_insert_with(|| value.clone());
    }

    for key in ["outputStyle", "spinnerTipsEnabled"] {
        if !result_object.contains_key(key) {
            if let Some(value) = rendered.get(key) {
                result_object.insert(key.to_string(), value.clone());
            }
        }
    }
    result_object.remove("mcpServers");

    Ok((result, claim))
}

pub(crate) fn remove_settings_claim(
    existing: &Value,
    claim: &SettingsClaim,
) -> Result<Value, InstallError> {
    verify_settings_claim(existing, claim)?;
    remove_settings_claim_inner(existing, claim)
        .ok_or_else(|| InstallError::metadata("settings.json must be an object"))
}

/// Claim removal without the presence verification: removes whatever claimed
/// entries still exist and leaves everything else alone. Used by uninstall,
/// whose forward-recovery rerun may find entries already gone (the v3 arm
/// relied on transactional atomicity instead); a user-modified managed entry
/// is left in place rather than blocking the uninstall.
pub(crate) fn remove_settings_claim_lenient(existing: &Value, claim: &SettingsClaim) -> Value {
    remove_settings_claim_inner(existing, claim).unwrap_or_else(|| existing.clone())
}

fn remove_settings_claim_inner(existing: &Value, claim: &SettingsClaim) -> Option<Value> {
    let mut result = existing.clone();
    let result_object = result.as_object_mut()?;
    if let Some(hooks) = result_object
        .get_mut("hooks")
        .and_then(Value::as_object_mut)
    {
        for item in &claim.hooks {
            if let Some(entries) = hooks.get_mut(&item.event).and_then(Value::as_array_mut) {
                for entry in entries.iter_mut() {
                    let Some(entry) = entry.as_object_mut() else {
                        continue;
                    };
                    if string_view(entry.get("matcher")) != item.matcher {
                        continue;
                    }
                    if let Some(list) = entry.get_mut("hooks").and_then(Value::as_array_mut) {
                        list.retain(|hook| {
                            !hook
                                .as_object()
                                .and_then(|h| h.get("command"))
                                .and_then(Value::as_str)
                                .is_some_and(|c| c.trim() == item.command)
                        });
                    }
                }
                entries.retain(|entry| {
                    entry
                        .as_object()
                        .and_then(|e| e.get("hooks"))
                        .and_then(Value::as_array)
                        .is_some_and(|list| !list.is_empty())
                });
                if entries.is_empty() {
                    hooks.remove(&item.event);
                }
            }
        }
        if hooks.is_empty() {
            result_object.remove("hooks");
        }
    }
    if let Some(permissions) = result_object
        .get_mut("permissions")
        .and_then(Value::as_object_mut)
    {
        if let Some(allow) = permissions.get_mut("allow").and_then(Value::as_array_mut) {
            allow.retain(|item| {
                !item
                    .as_str()
                    .is_some_and(|text| claim.permissions.iter().any(|p| p == text))
            });
            if allow.is_empty() {
                permissions.remove("allow");
            }
        }
        if permissions.is_empty() {
            result_object.remove("permissions");
        }
    }
    Some(result)
}

pub(crate) fn verify_settings_claim(
    settings: &Value,
    claim: &SettingsClaim,
) -> Result<(), InstallError> {
    let hooks = match settings.get("hooks") {
        Some(value) => value
            .as_object()
            .ok_or_else(|| InstallError::metadata("settings hooks must be an object"))?,
        None => &Map::new(),
    };
    for item in &claim.hooks {
        let entries = hooks.get(&item.event).and_then(Value::as_array);
        let found = entries.is_some_and(|entries| {
            entries.iter().any(|entry| {
                entry.as_object().is_some_and(|entry| {
                    string_view(entry.get("matcher")) == item.matcher
                        && entry
                            .get("hooks")
                            .and_then(Value::as_array)
                            .is_some_and(|list| {
                                list.iter().any(|hook| {
                                    hook.as_object()
                                        .and_then(|h| h.get("command"))
                                        .and_then(Value::as_str)
                                        .is_some_and(|c| c.trim() == item.command)
                                })
                            })
                })
            })
        });
        if !found {
            return Err(InstallError::runtime(
                "a managed settings Hook was modified",
            ));
        }
    }
    let permissions_section = match settings.get("permissions") {
        Some(value) => value
            .as_object()
            .ok_or_else(|| InstallError::metadata("settings permissions must be an object"))?,
        None => &Map::new(),
    };
    let allow = match permissions_section.get("allow") {
        Some(value) => value
            .as_array()
            .ok_or_else(|| InstallError::metadata("settings permissions.allow must be an array"))?,
        None => &Vec::new(),
    };
    for permission in &claim.permissions {
        if !allow.iter().any(|item| item.as_str() == Some(permission)) {
            return Err(InstallError::runtime(
                "a managed settings permission was modified",
            ));
        }
    }
    Ok(())
}

fn render_template(template: &Value, claude_root: &Path, commands: &HookCommands) -> Value {
    let mut rendered = template.clone();
    let claude_prefix = format!("{}/", claude_root.to_string_lossy().replace('\\', "/"));
    if let Some(hooks) = rendered.get_mut("hooks").and_then(Value::as_object_mut) {
        for entries in hooks.values_mut() {
            let Some(entries) = entries.as_array_mut() else {
                continue;
            };
            for entry in entries {
                let Some(list) = entry.get_mut("hooks").and_then(Value::as_array_mut) else {
                    continue;
                };
                for hook in list {
                    let Some(hook) = hook.as_object_mut() else {
                        continue;
                    };
                    let command = hook.get("command").and_then(Value::as_str).unwrap_or("");
                    let command = command
                        .replace(ENRICH_PLACEHOLDER, &commands.enrich)
                        .replace(DIRTY_PLACEHOLDER, &commands.dirty)
                        .replace("~/.claude/", &claude_prefix);
                    hook.insert("command".to_string(), Value::String(command));
                }
            }
        }
    }
    rendered
}

fn remove_prior_target_hooks(
    settings: &mut Value,
    claude_root: &Path,
    remy_root: &Path,
    prior_claim: Option<&SettingsClaim>,
) -> Result<(), InstallError> {
    let Some(settings) = settings.as_object_mut() else {
        return Err(InstallError::metadata("settings.json must be an object"));
    };
    let Some(hooks_value) = settings.get_mut("hooks") else {
        return Ok(());
    };
    let hooks = hooks_value
        .as_object_mut()
        .ok_or_else(|| InstallError::metadata("settings hooks must be an object"))?;
    let claimed: Vec<String> = prior_claim
        .map(|claim| {
            claim
                .hooks
                .iter()
                .map(|h| h.command.trim().to_string())
                .collect()
        })
        .unwrap_or_default();
    for (event, matcher, script_name) in TARGET_HOOKS {
        let Some(entries) = hooks.get_mut(*event).and_then(Value::as_array_mut) else {
            continue;
        };
        for entry in entries.iter_mut() {
            let Some(entry) = entry.as_object_mut() else {
                continue;
            };
            if string_view(entry.get("matcher")) != *matcher {
                continue;
            }
            let Some(list) = entry.get_mut("hooks").and_then(Value::as_array_mut) else {
                continue;
            };
            let mut retained = Vec::new();
            for hook in list.drain(..) {
                let command = hook
                    .as_object()
                    .and_then(|h| h.get("command"))
                    .and_then(Value::as_str)
                    .map(|c| c.trim().to_string());
                let Some(command) = command else {
                    retained.push(hook);
                    continue;
                };
                if claimed.contains(&command)
                    || is_legacy_default(&command, claude_root, script_name)
                    || is_legacy_daemon_default(&command, remy_root)
                {
                    continue;
                }
                if command.contains(script_name)
                    || (command.contains("remy-cc") && command.contains(" hook "))
                {
                    return Err(InstallError::runtime(
                        "an existing target Hook command was modified",
                    ));
                }
                retained.push(hook);
            }
            *list = retained;
        }
        entries.retain(|entry| {
            entry
                .as_object()
                .and_then(|e| e.get("hooks"))
                .and_then(Value::as_array)
                .is_some_and(|list| !list.is_empty())
        });
        if entries.is_empty() {
            hooks.remove(*event);
        }
    }
    Ok(())
}

fn is_legacy_default(command: &str, claude_root: &Path, script_name: &str) -> bool {
    let normalized = command.replace('\\', "/");
    let absolute = claude_root
        .join("hooks")
        .join(script_name)
        .to_string_lossy()
        .replace('\\', "/");
    normalized == format!("python \"~/.claude/hooks/{script_name}\"")
        || normalized == format!("python \"{absolute}\"")
}

/// Clears the pre-rename managed defaults (`"<remy>/bin/remy-daemon[.exe]"
/// hook enrich|dirty`) the same way the python-script defaults are cleared.
fn is_legacy_daemon_default(command: &str, remy_root: &Path) -> bool {
    let executable = remy_root.join("bin").join(legacy_exe_name(remy_root));
    let Ok(prefix) = quote_command_arg(&executable.to_string_lossy()) else {
        return false;
    };
    let normalized = command.replace('\\', "/");
    let prefix = prefix.replace('\\', "/");
    normalized == format!("{prefix} hook enrich") || normalized == format!("{prefix} hook dirty")
}

/// Python `str(value)` coercion used for matcher comparison: strings pass
/// through, anything else takes its JSON rendering.
fn string_view(value: Option<&Value>) -> String {
    match value {
        None => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(other) => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::install::ErrorKind;
    use serde_json::json;
    use std::path::PathBuf;

    fn roots() -> (PathBuf, PathBuf) {
        if cfg!(windows) {
            (
                PathBuf::from("C:\\home\\.claude"),
                PathBuf::from("C:\\home\\.remy-cc"),
            )
        } else {
            (
                PathBuf::from("/home/user/.claude"),
                PathBuf::from("/home/user/.remy-cc"),
            )
        }
    }

    fn template() -> Value {
        json!({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Read|Glob|Grep",
                    "hooks": [{"type": "command", "command": ENRICH_PLACEHOLDER}],
                }],
                "PostToolUse": [{
                    "matcher": "Edit|Write",
                    "hooks": [{"type": "command", "command": DIRTY_PLACEHOLDER}],
                }],
                "UserPromptSubmit": [{
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "python \"~/.claude/hooks/env_system/enforcer_hook.py\""}],
                }],
            },
            "permissions": {"allow": ["Skill(remy-index)", "Skill(remy-plan)"]},
            "env": {"REMY_LANG": "en"},
            "outputStyle": "system-architect",
            "spinnerTipsEnabled": false,
        })
    }

    fn merge(existing: Value, prior: Option<&SettingsClaim>) -> (Value, SettingsClaim) {
        let (claude, remy) = roots();
        let commands = hook_commands(&remy).expect("commands");
        merge_settings_document(&existing, &template(), &claude, &remy, &commands, prior)
            .expect("merge")
    }

    fn all_commands(settings: &Value) -> Vec<String> {
        let mut commands = Vec::new();
        for entries in settings["hooks"].as_object().expect("hooks").values() {
            for entry in entries.as_array().expect("entries") {
                for hook in entry["hooks"].as_array().expect("hooks list") {
                    commands.push(hook["command"].as_str().expect("command").to_string());
                }
            }
        }
        commands
    }

    #[test]
    fn merge_renders_managed_commands_and_expands_tilde() {
        let (merged, claim) = merge(json!({}), None);
        let commands = all_commands(&merged);
        assert!(commands
            .iter()
            .any(|c| c.contains("remy-cc") && c.ends_with("hook enrich")));
        assert!(commands
            .iter()
            .any(|c| c.contains("remy-cc") && c.ends_with("hook dirty")));
        let (claude, _) = roots();
        let prefix = claude.to_string_lossy().replace('\\', "/");
        assert!(commands
            .iter()
            .any(|c| c.contains(&prefix) && c.contains("enforcer_hook.py")));
        assert!(!commands.iter().any(|c| c.contains("~/.claude/")));
        assert_eq!(claim.hooks.len(), 3);
        assert_eq!(claim.permissions.len(), 2);
        assert_eq!(merged["outputStyle"], "system-architect");
        assert_eq!(merged["spinnerTipsEnabled"], false);
        assert_eq!(merged["env"]["REMY_LANG"], "en");
    }

    #[test]
    fn merge_is_idempotent_under_prior_claim() {
        let (first, claim) = merge(json!({}), None);
        let (second, second_claim) = merge(first.clone(), Some(&claim));
        assert_eq!(first, second);
        assert_eq!(claim, second_claim);
    }

    #[test]
    fn merge_preserves_user_entries_and_existing_env() {
        let existing = json!({
            "hooks": {"PreToolUse": [{
                "matcher": "Read|Glob|Grep",
                "hooks": [{"type": "command", "command": "python user_hook.py"}],
            }]},
            "permissions": {"allow": ["Skill(user-skill)"]},
            "env": {"REMY_LANG": "zh-CN"},
            "outputStyle": "user-style",
            "mcpServers": {"stale": {}},
        });
        let (merged, claim) = merge(existing, None);
        let commands = all_commands(&merged);
        assert!(commands.iter().any(|c| c == "python user_hook.py"));
        assert_eq!(merged["env"]["REMY_LANG"], "zh-CN");
        assert_eq!(merged["outputStyle"], "user-style");
        let allow = merged["permissions"]["allow"].as_array().expect("allow");
        assert!(allow.iter().any(|p| p == "Skill(user-skill)"));
        assert!(merged.get("mcpServers").is_none());
        assert!(!claim.permissions.contains(&"Skill(user-skill)".to_string()));
    }

    #[test]
    fn preexisting_template_permission_is_not_claimed() {
        let existing = json!({"permissions": {"allow": ["Skill(remy-index)"]}});
        let (_, claim) = merge(existing, None);
        assert!(!claim.permissions.contains(&"Skill(remy-index)".to_string()));
        assert!(claim.permissions.contains(&"Skill(remy-plan)".to_string()));
    }

    #[test]
    fn previously_claimed_permission_stays_claimed_on_reinstall() {
        let (first, claim) = merge(json!({}), None);
        let (_, second_claim) = merge(first, Some(&claim));
        assert!(second_claim
            .permissions
            .contains(&"Skill(remy-index)".to_string()));
    }

    #[test]
    fn modified_managed_hook_rejects_with_python_message() {
        let (mut first, claim) = merge(json!({}), None);
        let command = first["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            .as_str()
            .expect("command")
            .to_string();
        first["hooks"]["PreToolUse"][0]["hooks"][0]["command"] =
            Value::String(format!("{command} --user-change"));
        let error = verify_settings_claim(&first, &claim).expect_err("modified");
        assert_eq!(error.message, "a managed settings Hook was modified");
        assert_eq!(error.kind, ErrorKind::Runtime);
    }

    #[test]
    fn structurally_invalid_hooks_reject_as_metadata() {
        let claim = SettingsClaim::default();
        let error = verify_settings_claim(&json!({"hooks": []}), &claim).expect_err("invalid");
        assert_eq!(error.message, "settings hooks must be an object");
        assert_eq!(error.kind, ErrorKind::Metadata);
        let (claude, remy) = roots();
        let commands = hook_commands(&remy).expect("commands");
        let error = merge_settings_document(
            &json!({"hooks": []}),
            &template(),
            &claude,
            &remy,
            &commands,
            None,
        )
        .expect_err("invalid");
        assert_eq!(error.message, "settings hooks must be an object");
    }

    #[test]
    fn legacy_python_defaults_are_cleared_silently() {
        let (claude, _) = roots();
        let absolute = claude
            .join("hooks")
            .join("logic_enrichment_hook.py")
            .to_string_lossy()
            .replace('\\', "/");
        let existing = json!({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Read|Glob|Grep",
                    "hooks": [
                        {"type": "command", "command": "python \"~/.claude/hooks/logic_enrichment_hook.py\""},
                        {"type": "command", "command": format!("python \"{absolute}\"")},
                    ],
                }],
                "PostToolUse": [{
                    "matcher": "Edit|Write",
                    "hooks": [{"type": "command", "command": "python \"~/.claude/hooks/logic_dirty_tracker.py\""}],
                }],
            },
        });
        let (merged, _) = merge(existing, None);
        let commands = all_commands(&merged);
        assert!(!commands
            .iter()
            .any(|c| c.contains("logic_enrichment_hook.py")));
        assert!(!commands
            .iter()
            .any(|c| c.contains("logic_dirty_tracker.py")));
        assert!(commands.iter().any(|c| c.ends_with("hook enrich")));
    }

    #[test]
    fn legacy_daemon_defaults_are_cleared_silently() {
        let (_, remy) = roots();
        let stem = if cfg!(windows) {
            "remy-daemon.exe"
        } else {
            "remy-daemon"
        };
        let old = remy.join("bin").join(stem).to_string_lossy().into_owned();
        let existing = json!({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Read|Glob|Grep",
                    "hooks": [{"type": "command", "command": format!("\"{old}\" hook enrich")}],
                }],
                "PostToolUse": [{
                    "matcher": "Edit|Write",
                    "hooks": [{"type": "command", "command": format!("\"{old}\" hook dirty")}],
                }],
            },
        });
        let (merged, _) = merge(existing, None);
        let commands = all_commands(&merged);
        assert!(!commands.iter().any(|c| c.contains(LEGACY_DAEMON_STEM)));
        assert!(commands.iter().any(|c| c.ends_with("hook enrich")));
        assert!(commands.iter().any(|c| c.ends_with("hook dirty")));
    }

    #[test]
    fn unrecognized_target_shaped_command_rejects() {
        let existing = json!({
            "hooks": {"PreToolUse": [{
                "matcher": "Read|Glob|Grep",
                "hooks": [{"type": "command", "command": "\"/elsewhere/remy-cc\" hook enrich --extra"}],
            }]},
        });
        let (claude, remy) = roots();
        let commands = hook_commands(&remy).expect("commands");
        let error =
            merge_settings_document(&existing, &template(), &claude, &remy, &commands, None)
                .expect_err("modified target");
        assert_eq!(
            error.message,
            "an existing target Hook command was modified"
        );
    }

    #[test]
    fn remove_claim_deletes_managed_entries_and_keeps_user_data() {
        let existing = json!({
            "hooks": {"PreToolUse": [{
                "matcher": "Read|Glob|Grep",
                "hooks": [{"type": "command", "command": "python user_hook.py"}],
            }]},
            "permissions": {"allow": ["Skill(user-skill)"]},
        });
        let (merged, claim) = merge(existing, None);
        let cleaned = remove_settings_claim(&merged, &claim).expect("remove");
        let commands = all_commands(&cleaned);
        assert_eq!(commands, vec!["python user_hook.py".to_string()]);
        let allow = cleaned["permissions"]["allow"].as_array().expect("allow");
        assert_eq!(allow.len(), 1);
        assert_eq!(allow[0], "Skill(user-skill)");
    }

    #[test]
    fn remove_claim_drops_empty_sections_entirely() {
        let (merged, claim) = merge(json!({}), None);
        let cleaned = remove_settings_claim(&merged, &claim).expect("remove");
        assert!(cleaned.get("hooks").is_none());
        assert!(cleaned.get("permissions").is_none());
    }

    #[test]
    fn remove_claim_tolerates_missing_permissions_section() {
        let claim = SettingsClaim {
            hooks: Vec::new(),
            permissions: Vec::new(),
        };
        let cleaned = remove_settings_claim(&json!({"env": {}}), &claim).expect("remove");
        assert_eq!(cleaned, json!({"env": {}}));
    }

    #[test]
    fn quote_rejects_unsupported_characters() {
        assert!(quote_command_arg("C:\\path\\bin").is_ok());
        for bad in ["has\"quote", "line\nbreak", "carriage\rreturn"] {
            let error = quote_command_arg(bad).expect_err("rejected");
            assert_eq!(
                error.message,
                "managed command path contains unsupported characters"
            );
        }
    }

    #[test]
    fn claim_round_trips_through_json() {
        let (_, claim) = merge(json!({}), None);
        let value = claim.to_value();
        let parsed = SettingsClaim::from_value(&value).expect("parse");
        assert_eq!(parsed, claim);
    }
}
