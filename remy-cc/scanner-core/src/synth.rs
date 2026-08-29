//! synthesizers/ replication: event emitter, interface override, C
//! function-pointer dispatch (tee profile), and Rust trait-impl synthesis.
//! Every inserted row carries provenance='inferred' and the same
//! synthesized_from/via values as the Python pass; run order copies
//! run_all_synthesizers.

use crate::rconfig::PostprocessConfig;
use rusqlite::{params, Transaction};
use serde_json::Value;
use std::collections::{BTreeSet, HashMap, HashSet};

type SignalRow = (String, Option<String>, Option<String>, Option<i64>);
type DispatchRow = (
    Option<String>,
    Option<String>,
    Option<String>,
    String,
    Option<i64>,
);

/// c_fnptr_profiles/tee.py TEE_PROFILE["fanout_cap"].
const C_FNPTR_TEE_FANOUT_CAP: usize = 300;

pub fn run_all(tx: &Transaction, config: &PostprocessConfig) -> rusqlite::Result<()> {
    synthesize_event_emitter_edges(tx, config.synth_event_fanout_cap as usize)?;
    synthesize_interface_override_edges(tx, config.synth_interface_fanout_cap as usize)?;
    synthesize_c_fnptr_dispatch_edges(tx)?;
    synthesize_rust_trait_impl_edges(tx, config.synth_interface_fanout_cap as usize)?;
    Ok(())
}

struct InferredEdge<'a> {
    source_file: &'a str,
    caller: &'a str,
    callee: &'a str,
    callee_file: &'a str,
    callee_qualified: &'a str,
    line: i64,
    synthesized_from: &'a str,
    via: &'a str,
}

fn insert_inferred(tx: &Transaction, edge: &InferredEdge) -> rusqlite::Result<usize> {
    tx.execute(
        "INSERT OR IGNORE INTO edges \
         (source_file, caller, callee, callee_file, callee_qualified, \
         line, provenance, synthesized_from, via) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
        params![
            edge.source_file,
            edge.caller,
            edge.callee,
            edge.callee_file,
            edge.callee_qualified,
            edge.line,
            "inferred",
            edge.synthesized_from,
            edge.via,
        ],
    )
}

fn parse_bases(bases_json: &str) -> Option<Vec<String>> {
    let value: Value = serde_json::from_str(bases_json).ok()?;
    let items = value.as_array()?;
    Some(
        items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect(),
    )
}

fn method_shorts(
    tx: &Transaction,
    file_path: &str,
    prefix: &str,
) -> rusqlite::Result<HashSet<String>> {
    let mut stmt = tx.prepare(
        "SELECT name FROM symbols WHERE file_path = ?1 AND type = 'function' \
         AND name LIKE ?2 ORDER BY name",
    )?;
    let names: Vec<String> = stmt
        .query_map(params![file_path, format!("{prefix}.%")], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    Ok(names
        .into_iter()
        .map(|name| name.rsplit('.').next().unwrap_or(&name).to_string())
        .collect())
}

fn methods_by_line(
    tx: &Transaction,
    file_path: &str,
    prefix: &str,
) -> rusqlite::Result<Vec<(String, Option<i64>)>> {
    let mut stmt = tx.prepare(
        "SELECT name, lineno FROM symbols WHERE file_path = ?1 \
         AND type = 'function' AND name LIKE ?2 \
         ORDER BY COALESCE(lineno, 0), name",
    )?;
    let collected = stmt
        .query_map(params![file_path, format!("{prefix}.%")], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })?
        .collect();
    collected
}

/// `interface_dispatch.synthesize_interface_override_edges`.
fn synthesize_interface_override_edges(
    tx: &Transaction,
    fanout_cap: usize,
) -> rusqlite::Result<()> {
    let classes: Vec<(String, String, String)> = {
        let mut stmt = tx.prepare(
            "SELECT file_path, name, bases FROM symbols \
             WHERE type = 'class' AND bases IS NOT NULL ORDER BY file_path, name",
        )?;
        let collected = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
            .collect::<Result<_, _>>()?;
        collected
    };

    let mut seen: HashSet<(String, String, String, &'static str)> = HashSet::new();
    for (impl_path, impl_class, bases_json) in classes {
        let Some(bases) = parse_bases(&bases_json).filter(|bases| !bases.is_empty()) else {
            continue;
        };
        let impl_method_set = method_shorts(tx, &impl_path, &impl_class)?;
        if impl_method_set.is_empty() {
            continue;
        }
        for base_name in bases.into_iter().collect::<BTreeSet<_>>() {
            let base_classes: Vec<(String, String)> = {
                let mut stmt = tx.prepare(
                    "SELECT file_path, name FROM symbols WHERE type = 'class' \
                     AND short_name = ?1 ORDER BY file_path, name",
                )?;
                let collected = stmt
                    .query_map(params![base_name], |row| Ok((row.get(0)?, row.get(1)?)))?
                    .collect::<Result<_, _>>()?;
                collected
            };
            for (base_path, base_full_name) in base_classes {
                let base_methods = methods_by_line(tx, &base_path, &base_full_name)?;
                if base_methods.is_empty() {
                    continue;
                }
                let mut added = 0usize;
                for (base_method_name, base_lineno) in base_methods {
                    if added >= fanout_cap {
                        break;
                    }
                    let method_short = base_method_name.rsplit('.').next().unwrap_or("");
                    if !impl_method_set.contains(method_short) {
                        continue;
                    }
                    let impl_qualified = format!("{impl_path}::{impl_class}.{method_short}");
                    let key = (
                        base_path.clone(),
                        base_method_name.clone(),
                        impl_qualified.clone(),
                        "interface-impl",
                    );
                    if !seen.insert(key) {
                        continue;
                    }
                    insert_inferred(
                        tx,
                        &InferredEdge {
                            source_file: &base_path,
                            caller: &base_method_name,
                            callee: &format!("{impl_class}.{method_short}"),
                            callee_file: &impl_path,
                            callee_qualified: &impl_qualified,
                            line: base_lineno.unwrap_or(0),
                            synthesized_from: &base_path,
                            via: "interface-impl",
                        },
                    )?;
                    added += 1;
                }
            }
        }
    }
    Ok(())
}

/// `rust_trait_dispatch.synthesize_rust_trait_impl_edges` (patterns-driven).
fn synthesize_rust_trait_impl_edges(tx: &Transaction, fanout_cap: usize) -> rusqlite::Result<()> {
    let impls: Vec<(String, String, String)> = {
        let mut stmt = tx.prepare(
            "SELECT file_path, signal_name, handler FROM patterns \
             WHERE pattern_type = 'rust_trait_impl' \
             AND signal_name IS NOT NULL AND handler IS NOT NULL \
             ORDER BY file_path, COALESCE(line, 0), signal_name, handler",
        )?;
        let collected = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
            .collect::<Result<_, _>>()?;
        collected
    };

    let mut seen: HashSet<(String, String, String, &'static str)> = HashSet::new();
    for (impl_file, trait_name, full_type) in impls {
        let impl_method_set = method_shorts(tx, &impl_file, &full_type)?;
        if impl_method_set.is_empty() {
            continue;
        }
        let traits: Vec<(String, String)> = {
            let mut stmt = tx.prepare(
                "SELECT file_path, name FROM symbols WHERE type = 'interface' \
                 AND short_name = ?1 AND file_path LIKE '%.rs' \
                 ORDER BY file_path, name",
            )?;
            let collected = stmt
                .query_map(params![trait_name], |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect::<Result<_, _>>()?;
            collected
        };
        for (trait_path, trait_full_name) in traits {
            let trait_methods = methods_by_line(tx, &trait_path, &trait_full_name)?;
            if trait_methods.is_empty() {
                continue;
            }
            let mut added = 0usize;
            for (trait_method_name, trait_lineno) in trait_methods {
                if added >= fanout_cap {
                    break;
                }
                let method_short = trait_method_name.rsplit('.').next().unwrap_or("");
                if !impl_method_set.contains(method_short) {
                    continue;
                }
                let impl_qualified = format!("{impl_file}::{full_type}.{method_short}");
                let key = (
                    trait_path.clone(),
                    trait_method_name.clone(),
                    impl_qualified.clone(),
                    "trait-impl",
                );
                if !seen.insert(key) {
                    continue;
                }
                insert_inferred(
                    tx,
                    &InferredEdge {
                        source_file: &trait_path,
                        caller: &trait_method_name,
                        callee: &format!("{full_type}.{method_short}"),
                        callee_file: &impl_file,
                        callee_qualified: &impl_qualified,
                        line: trait_lineno.unwrap_or(0),
                        synthesized_from: &trait_path,
                        via: "trait-impl",
                    },
                )?;
                added += 1;
            }
        }
    }
    Ok(())
}

/// `event_emitter.synthesize_event_emitter_edges`.
fn synthesize_event_emitter_edges(tx: &Transaction, fanout_cap: usize) -> rusqlite::Result<()> {
    for (emit_type, connect_type, via) in [
        (
            "django_signal_send",
            "django_signal_connect",
            "django-signal",
        ),
        ("pyqt_signal_emit", "pyqt_signal_connect", "pyqt-signal"),
        ("observer_emit", "observer_register", "observer"),
    ] {
        synthesize_signal_pattern(tx, emit_type, connect_type, via, fanout_cap)?;
    }
    Ok(())
}

fn synthesize_signal_pattern(
    tx: &Transaction,
    emit_type: &str,
    connect_type: &str,
    via: &str,
    fanout_cap: usize,
) -> rusqlite::Result<()> {
    let emitters: Vec<SignalRow> = {
        let mut stmt = tx.prepare(
            "SELECT file_path, signal_name, handler, line FROM patterns \
             WHERE pattern_type = ?1 \
             ORDER BY COALESCE(line, 0), file_path, signal_name, handler",
        )?;
        let collected = stmt
            .query_map(params![emit_type], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            })?
            .collect::<Result<_, _>>()?;
        collected
    };
    if emitters.is_empty() {
        return Ok(());
    }
    let signal_names: Vec<String> = emitters
        .iter()
        .filter_map(|(_, signal, _, _)| signal.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if signal_names.is_empty() {
        return Ok(());
    }

    let placeholders = vec!["?"; signal_names.len()].join(",");
    let sql = format!(
        "SELECT file_path, signal_name, handler, line FROM patterns \
         WHERE pattern_type = ? AND signal_name IN ({placeholders}) \
         ORDER BY signal_name, COALESCE(line, 0), file_path, handler"
    );
    let handlers: Vec<SignalRow> = {
        let mut stmt = tx.prepare(&sql)?;
        let bound: Vec<&str> = std::iter::once(connect_type)
            .chain(signal_names.iter().map(String::as_str))
            .collect();
        let collected = stmt
            .query_map(rusqlite::params_from_iter(bound), |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            })?
            .collect::<Result<_, _>>()?;
        collected
    };

    let mut handler_map: HashMap<String, Vec<(String, Option<String>)>> = HashMap::new();
    for (h_path, h_signal, h_func, _line) in handlers {
        if let Some(signal) = h_signal {
            handler_map
                .entry(signal)
                .or_default()
                .push((h_path, h_func));
        }
    }

    let mut seen: HashSet<(String, String, String, String)> = HashSet::new();
    for (e_path, e_signal, e_func, e_line) in emitters {
        let (Some(signal), Some(func)) = (e_signal, e_func) else {
            continue;
        };
        let Some(targets) = handler_map.get(&signal) else {
            continue;
        };
        if targets.len() > fanout_cap {
            continue;
        }
        for (h_path, h_func) in targets {
            let Some(h_func) = h_func else {
                continue;
            };
            if e_path == *h_path && func == *h_func {
                continue;
            }
            let qualified = format!("{h_path}::{h_func}");
            let key = (
                e_path.clone(),
                func.clone(),
                qualified.clone(),
                via.to_string(),
            );
            if !seen.insert(key) {
                continue;
            }
            insert_inferred(
                tx,
                &InferredEdge {
                    source_file: &e_path,
                    caller: &func,
                    callee: h_func,
                    callee_file: h_path,
                    callee_qualified: &qualified,
                    line: e_line.unwrap_or(0),
                    synthesized_from: &e_path,
                    via,
                },
            )?;
        }
    }
    Ok(())
}

fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().is_some_and(|n| n != 0.0),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

/// `c_fnptr_dispatch.synthesize_c_fnptr_dispatch_edges` (tee profile).
fn synthesize_c_fnptr_dispatch_edges(tx: &Transaction) -> rusqlite::Result<()> {
    let fanout_cap = C_FNPTR_TEE_FANOUT_CAP;

    let mut fnptr_typedefs: HashSet<String> = HashSet::new();
    {
        let mut stmt = tx.prepare(
            "SELECT signal_name FROM patterns WHERE pattern_type = 'c_fnptr_typedef' \
             ORDER BY signal_name",
        )?;
        let rows: Vec<Option<String>> = stmt
            .query_map([], |row| row.get(0))?
            .collect::<Result<_, _>>()?;
        for signal in rows.into_iter().flatten() {
            fnptr_typedefs.insert(signal);
        }
    }

    let mut layouts: HashMap<String, Vec<Vec<Value>>> = HashMap::new();
    {
        let mut stmt = tx.prepare(
            "SELECT signal_name, metadata FROM patterns \
             WHERE pattern_type = 'c_struct_layout' \
             ORDER BY signal_name, metadata",
        )?;
        let rows: Vec<(Option<String>, Option<String>)> = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<Result<_, _>>()?;
        for (signal, metadata) in rows {
            let (Some(signal), Some(metadata)) = (signal, metadata) else {
                continue;
            };
            let Some(fields) = serde_json::from_str::<Value>(&metadata)
                .ok()
                .and_then(|doc| doc.get("fields").and_then(Value::as_array).cloned())
            else {
                continue;
            };
            let entry = layouts.entry(signal).or_default();
            if !fields.is_empty() && !entry.contains(&fields) {
                entry.push(fields);
            }
        }
    }

    let field_fnptr = |field: &Value| -> bool {
        truthy(field.get("is_fnptr"))
            || field
                .get("type")
                .and_then(Value::as_str)
                .is_some_and(|t| fnptr_typedefs.contains(t))
    };

    let resolve_reg_field = |structure: &str, field_name: Option<&str>, slot: Option<i64>| {
        for fields in layouts.get(structure).map(Vec::as_slice).unwrap_or(&[]) {
            for field in fields {
                if let Some(field_name) = field_name {
                    if field.get("name").and_then(Value::as_str) == Some(field_name)
                        && field_fnptr(field)
                    {
                        return field
                            .get("name")
                            .and_then(Value::as_str)
                            .map(str::to_string);
                    }
                }
                if let Some(slot) = slot {
                    if field.get("index").and_then(Value::as_i64) == Some(slot)
                        && field_fnptr(field)
                    {
                        return field
                            .get("name")
                            .and_then(Value::as_str)
                            .map(str::to_string);
                    }
                }
            }
        }
        None
    };

    let mut field_to_structs: HashMap<String, HashSet<String>> = HashMap::new();
    for (structure, layout_list) in &layouts {
        for fields in layout_list {
            for field in fields {
                if let Some(name) = field.get("name").and_then(Value::as_str) {
                    if !name.is_empty() && field_fnptr(field) {
                        field_to_structs
                            .entry(name.to_string())
                            .or_default()
                            .insert(structure.clone());
                    }
                }
            }
        }
    }

    let mut reg: HashMap<(String, String), BTreeSet<String>> = HashMap::new();
    {
        let mut stmt = tx.prepare(
            "SELECT signal_name, handler, metadata FROM patterns \
             WHERE pattern_type = 'c_fnptr_register' \
             ORDER BY signal_name, handler, metadata",
        )?;
        let rows: Vec<(Option<String>, Option<String>, Option<String>)> = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
            .collect::<Result<_, _>>()?;
        for (signal, handler, metadata) in rows {
            let (Some(signal), Some(handler), Some(metadata)) = (signal, handler, metadata) else {
                continue;
            };
            let Ok(md) = serde_json::from_str::<Value>(&metadata) else {
                continue;
            };
            let field = resolve_reg_field(
                &signal,
                md.get("field").and_then(Value::as_str),
                md.get("slot").and_then(Value::as_i64),
            );
            if let Some(field) = field {
                reg.entry((signal, field)).or_default().insert(handler);
            }
        }
    }
    if reg.is_empty() {
        return Ok(());
    }

    let mut symbol_cache: HashMap<String, Vec<(String, String)>> = HashMap::new();
    let mut resolve_symbol = |tx: &Transaction,
                              name: &str,
                              prefer_file: Option<&str>|
     -> rusqlite::Result<Option<(String, String)>> {
        if !symbol_cache.contains_key(name) {
            let mut stmt = tx.prepare(
                "SELECT file_path, name FROM symbols WHERE (short_name = ?1 OR name = ?1) \
                 AND type IN ('function', 'macro') ORDER BY file_path, name",
            )?;
            let rows: Vec<(String, String)> = stmt
                .query_map(params![name], |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect::<Result<_, _>>()?;
            symbol_cache.insert(name.to_string(), rows);
        }
        let rows = &symbol_cache[name];
        if rows.is_empty() {
            return Ok(None);
        }
        if let Some(prefer) = prefer_file {
            for (file_path, symbol_name) in rows {
                if file_path == prefer {
                    return Ok(Some((file_path.clone(), symbol_name.clone())));
                }
            }
        }
        Ok(Some(rows[0].clone()))
    };

    let dispatches: Vec<DispatchRow> = {
        let mut stmt = tx.prepare(
            "SELECT signal_name, handler, metadata, file_path, line \
             FROM patterns WHERE pattern_type = 'c_fnptr_dispatch' \
             ORDER BY COALESCE(line, 0), file_path, signal_name, handler, metadata",
        )?;
        let collected = stmt
            .query_map([], |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            })?
            .collect::<Result<_, _>>()?;
        collected
    };

    let mut seen: HashSet<(String, String, String, &'static str)> = HashSet::new();
    for (field, enclosing, metadata, disp_file, line) in dispatches {
        let (Some(field), Some(enclosing)) = (field, enclosing) else {
            continue;
        };
        let Some(owners) = field_to_structs.get(&field) else {
            continue;
        };
        let struct_hint = metadata
            .as_deref()
            .and_then(|text| serde_json::from_str::<Value>(text).ok())
            .and_then(|md| {
                md.get("struct_hint")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            });
        let structure = match struct_hint {
            Some(hint) if owners.contains(&hint) => hint,
            _ if owners.len() == 1 => owners.iter().next().unwrap().clone(),
            _ => continue,
        };
        let Some(targets) = reg.get(&(structure, field)) else {
            continue;
        };
        let Some((caller_file, caller_name)) = resolve_symbol(tx, &enclosing, Some(&disp_file))?
        else {
            continue;
        };
        let mut added = 0usize;
        for handler in targets {
            if added >= fanout_cap {
                break;
            }
            let Some((callee_file, callee_name)) = resolve_symbol(tx, handler, None)? else {
                continue;
            };
            if caller_file == callee_file && caller_name == callee_name {
                continue;
            }
            let qualified = format!("{callee_file}::{callee_name}");
            let key = (
                caller_file.clone(),
                caller_name.clone(),
                qualified.clone(),
                "c-fnptr-dispatch",
            );
            if !seen.insert(key) {
                continue;
            }
            insert_inferred(
                tx,
                &InferredEdge {
                    source_file: &caller_file,
                    caller: &caller_name,
                    callee: callee_name.rsplit('.').next().unwrap_or(&callee_name),
                    callee_file: &callee_file,
                    callee_qualified: &qualified,
                    line: line.unwrap_or(0),
                    synthesized_from: &disp_file,
                    via: "c-fnptr-dispatch",
                },
            )?;
            added += 1;
        }
    }
    Ok(())
}
