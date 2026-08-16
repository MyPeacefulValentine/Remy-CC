//! `ast.unparse` replication for the expressions that appear in a function
//! signature — the `symbols.args` column is an EXACT diff field and feeds
//! `symbol_selection`'s signature normalization, so the Python oracle's
//! canonicalized rendering (constant `repr`, operator spacing, precedence
//! parentheses, argument reordering) must be reproduced rather than the
//! source text echoed.
//!
//! Structure mirrors CPython's `ast._Unparser`: every expression is written
//! under a *context precedence* set by its parent, and a node parenthesizes
//! itself when that context outranks its own operator precedence.
//!
//! Nodes this module cannot render exactly (f-strings needing quote
//! renegotiation, `\N{NAME}` escapes, radix integers beyond `i128`, syntax
//! newer than the mapping below) fall back to the verbatim source slice.
//! A wrong-but-plausible rendering would corrupt symbol selection, while a
//! fallback only differs when the source was not already canonical — and the
//! cross-implementation diff surfaces those.

use crate::py_repr::{self, LiteralValue};
use tree_sitter::Node;

/// CPython `ast._Precedence`, including `BOR == EXPR`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Prec {
    NamedExpr = 1,
    Tuple = 2,
    Yield = 3,
    Test = 4,
    Or = 5,
    And = 6,
    Not = 7,
    Cmp = 8,
    Expr = 9,
    Bxor = 10,
    Band = 11,
    Shift = 12,
    Arith = 13,
    Term = 14,
    Factor = 15,
    Power = 16,
    Await = 17,
    Atom = 18,
}

/// `_Precedence.BOR` is an alias of `EXPR`.
const BOR: Prec = Prec::Expr;

impl Prec {
    /// `_Precedence.next()` — saturates at `ATOM`.
    fn next(self) -> Prec {
        match self {
            Prec::NamedExpr => Prec::Tuple,
            Prec::Tuple => Prec::Yield,
            Prec::Yield => Prec::Test,
            Prec::Test => Prec::Or,
            Prec::Or => Prec::And,
            Prec::And => Prec::Not,
            Prec::Not => Prec::Cmp,
            Prec::Cmp => Prec::Expr,
            Prec::Expr => Prec::Bxor,
            Prec::Bxor => Prec::Band,
            Prec::Band => Prec::Shift,
            Prec::Shift => Prec::Arith,
            Prec::Arith => Prec::Term,
            Prec::Term => Prec::Factor,
            Prec::Factor => Prec::Power,
            Prec::Power => Prec::Await,
            Prec::Await => Prec::Atom,
            Prec::Atom => Prec::Atom,
        }
    }
}

fn text<'a>(node: Node, source: &'a str) -> &'a str {
    source.get(node.byte_range()).unwrap_or("")
}

fn fallback(node: Node, source: &str) -> String {
    text(node, source).trim().to_string()
}

fn named_children<'a>(node: Node<'a>) -> Vec<Node<'a>> {
    let mut cursor = node.walk();
    node.named_children(&mut cursor).collect()
}

fn all_children<'a>(node: Node<'a>) -> Vec<Node<'a>> {
    let mut cursor = node.walk();
    node.children(&mut cursor).collect()
}

fn field_nodes<'a>(node: Node<'a>, field: &str) -> Vec<Node<'a>> {
    let mut out = Vec::new();
    let mut cursor = node.walk();
    if cursor.goto_first_child() {
        loop {
            if cursor.field_name() == Some(field) {
                out.push(cursor.node());
            }
            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }
    out
}

fn has_anonymous_child(node: Node, kind: &str) -> bool {
    all_children(node)
        .iter()
        .any(|child| !child.is_named() && child.kind() == kind)
}

/// The operator token of a `binary_operator`/`boolean_operator`/
/// `unary_operator` node: an anonymous child reached through the `operator`
/// field.
fn infix_operator<'a>(node: Node, source: &'a str) -> &'a str {
    match node.child_by_field_name("operator") {
        Some(operator) => text(operator, source),
        None => all_children(node)
            .iter()
            .find(|child| !child.is_named())
            .map(|child| text(*child, source))
            .unwrap_or(""),
    }
}

/// Strip transparent wrappers so a node can be classified the way its AST
/// counterpart would be (the AST has no parenthesized/annotation node).
fn unwrap_transparent(node: Node) -> Node {
    let mut current = node;
    while matches!(current.kind(), "parenthesized_expression" | "type") {
        match named_children(current).first() {
            Some(inner) => current = *inner,
            None => break,
        }
    }
    current
}

/// Render one expression with `_Precedence.TEST` as its context, the default
/// `_Unparser` starts from.
pub fn unparse_expression(node: Node, source: &str) -> String {
    expr(node, source, Prec::Test)
}

fn expr(node: Node, source: &str, ctx: Prec) -> String {
    match node.kind() {
        // The AST has no parenthesized node and wraps annotations bare, so
        // both are transparent: the parent's precedence applies inside.
        "parenthesized_expression" | "type" => match named_children(node).first() {
            Some(inner) => expr(*inner, source, ctx),
            None => fallback(node, source),
        },
        "identifier" => text(node, source).to_string(),
        "integer" => integer_literal(node, source),
        "float" => float_literal(node, source),
        "true" => "True".to_string(),
        "false" => "False".to_string(),
        "none" => "None".to_string(),
        "ellipsis" => "...".to_string(),
        "string" => string_literal(node, source),
        "concatenated_string" => concatenated_literal(node, source),
        "not_operator" => unary(node, source, ctx, "not", Prec::Not, "argument"),
        "unary_operator" => {
            let operator = infix_operator(node, source);
            unary(node, source, ctx, operator, Prec::Factor, "argument")
        }
        "binary_operator" => binary(node, source, ctx),
        "boolean_operator" => boolean(node, source, ctx),
        "comparison_operator" => comparison(node, source, ctx),
        "conditional_expression" => conditional(node, source, ctx),
        "lambda" => lambda(node, source, ctx),
        "call" => call(node, source),
        "attribute" => attribute(node, source),
        "subscript" => subscript(node, source),
        // A subscripted annotation parses as `generic_type` rather than
        // `subscript`, but the AST builds the same `Subscript` node.
        "generic_type" => generic_type(node, source),
        // `union_type` appears once an annotation's `|` chain contains a
        // subscripted member; the AST is still a flat left-associative
        // `BitOr` chain.
        "union_type" => union_type(node, source, ctx),
        "slice" => slice(node, source),
        "tuple" | "pattern_list" | "tuple_pattern" => tuple(node, source, ctx),
        "list" | "list_pattern" => format!("[{}]", comma_join(&named_children(node), source)),
        "set" => format!("{{{}}}", comma_join(&named_children(node), source)),
        "dictionary" => dictionary(node, source),
        "pair" => pair(node, source),
        "list_splat" | "list_splat_pattern" => splat(node, source, "*"),
        "dictionary_splat" | "dictionary_splat_pattern" => splat(node, source, "**"),
        "list_comprehension" => comprehension(node, source, "[", "]"),
        "set_comprehension" => comprehension(node, source, "{", "}"),
        "generator_expression" => comprehension(node, source, "(", ")"),
        "dictionary_comprehension" => comprehension(node, source, "{", "}"),
        "await" => await_expr(node, source, ctx),
        "yield" => yield_expr(node, source, ctx),
        "keyword_argument" => keyword_argument(node, source),
        "named_expression" => named_expression(node, source, ctx),
        _ => fallback(node, source),
    }
}

fn parenthesize(body: String, wrap: bool) -> String {
    if wrap {
        format!("({body})")
    } else {
        body
    }
}

fn comma_join(nodes: &[Node], source: &str) -> String {
    nodes
        .iter()
        .map(|node| expr(*node, source, Prec::Test))
        .collect::<Vec<_>>()
        .join(", ")
}

/// `_Unparser.items_view`: a single item keeps a trailing comma.
fn items_view(nodes: &[Node], source: &str) -> String {
    if nodes.len() == 1 {
        format!("{},", expr(nodes[0], source, Prec::Test))
    } else {
        comma_join(nodes, source)
    }
}

fn integer_literal(node: Node, source: &str) -> String {
    let raw = text(node, source);
    if let Some(stripped) = raw.strip_suffix(['j', 'J']) {
        return match parse_python_float(stripped) {
            Some(value) => py_repr::repr_imaginary(value),
            None => fallback(node, source),
        };
    }
    py_repr::repr_int_literal(raw).unwrap_or_else(|| fallback(node, source))
}

fn float_literal(node: Node, source: &str) -> String {
    let raw = text(node, source);
    if let Some(stripped) = raw.strip_suffix(['j', 'J']) {
        return match parse_python_float(stripped) {
            Some(value) => py_repr::repr_imaginary(value),
            None => fallback(node, source),
        };
    }
    match parse_python_float(raw) {
        Some(value) => py_repr::repr_float(value),
        None => fallback(node, source),
    }
}

fn parse_python_float(raw: &str) -> Option<f64> {
    let cleaned: String = raw.chars().filter(|c| *c != '_').collect();
    // Python allows `5.` and `.5`; Rust's parser accepts both.
    cleaned.parse::<f64>().ok()
}

fn string_literal(node: Node, source: &str) -> String {
    let raw = text(node, source);
    let Some((prefix, _quote, body)) = py_repr::split_literal(raw) else {
        return fallback(node, source);
    };
    if prefix.format {
        return format_string(node, source);
    }
    match py_repr::decode_literal_body(prefix, body) {
        Some(LiteralValue::Str(value)) => {
            let rendered = py_repr::repr_str(&value);
            if prefix.unicode {
                format!("u{rendered}")
            } else {
                rendered
            }
        }
        Some(LiteralValue::Bytes(value)) => py_repr::repr_bytes(&value),
        None => fallback(node, source),
    }
}

/// Adjacent literals are one `Constant` in the AST, so their decoded values
/// concatenate before `repr`.
fn concatenated_literal(node: Node, source: &str) -> String {
    let parts = named_children(node);
    let mut text_value = String::new();
    let mut bytes_value: Vec<u8> = Vec::new();
    let mut is_bytes = false;
    let mut is_unicode = false;
    for (index, part) in parts.iter().enumerate() {
        if part.kind() != "string" {
            return fallback(node, source);
        }
        let raw = text(*part, source);
        let Some((prefix, _quote, body)) = py_repr::split_literal(raw) else {
            return fallback(node, source);
        };
        if prefix.format {
            return fallback(node, source);
        }
        if index == 0 {
            is_bytes = prefix.bytes;
            is_unicode = prefix.unicode;
        } else if prefix.bytes != is_bytes {
            return fallback(node, source);
        }
        match py_repr::decode_literal_body(prefix, body) {
            Some(LiteralValue::Str(value)) => text_value.push_str(&value),
            Some(LiteralValue::Bytes(value)) => bytes_value.extend_from_slice(&value),
            None => return fallback(node, source),
        }
    }
    if is_bytes {
        py_repr::repr_bytes(&bytes_value)
    } else {
        let rendered = py_repr::repr_str(&text_value);
        if is_unicode {
            format!("u{rendered}")
        } else {
            rendered
        }
    }
}

/// f-strings: render the common shape exactly (single-quoted, no quote
/// renegotiation needed) and fall back otherwise. CPython picks the quote
/// character by testing every literal and expression part against the four
/// quote styles; reproducing that only pays off once a corpus diff shows an
/// f-string in a signature, and a fallback stays correct whenever the source
/// is already canonical.
fn format_string(node: Node, source: &str) -> String {
    let mut body = String::new();
    for child in all_children(node) {
        match child.kind() {
            "string_start" | "string_end" => {}
            "string_content" => {
                let raw = text(child, source);
                if raw.contains('\'')
                    || raw.contains('"')
                    || raw.contains('\\')
                    || raw.contains('\n')
                    || raw.contains('\t')
                    || raw.chars().any(|c| (c as u32) < 0x20)
                {
                    return fallback(node, source);
                }
                body.push_str(raw);
            }
            "interpolation" => match interpolation(child, source) {
                Some(rendered) => body.push_str(&rendered),
                None => return fallback(node, source),
            },
            _ => return fallback(node, source),
        }
    }
    format!("f'{body}'")
}

fn interpolation(node: Node, source: &str) -> Option<String> {
    let expression = node.child_by_field_name("expression")?;
    let rendered = expr(expression, source, Prec::Or);
    if rendered.contains('\'') || rendered.contains('"') || rendered.contains('\n') {
        return None;
    }
    let mut out = String::from("{");
    if rendered.starts_with('{') {
        out.push(' ');
    }
    out.push_str(&rendered);
    if let Some(conversion) = node.child_by_field_name("type_conversion") {
        out.push_str(text(conversion, source));
    }
    if let Some(spec) = node.child_by_field_name("format_specifier") {
        let raw = text(spec, source);
        if raw.contains('\'') || raw.contains('"') || raw.contains('\\') || raw.contains('\n') {
            return None;
        }
        out.push_str(raw);
    }
    out.push('}');
    Some(out)
}

fn unary(node: Node, source: &str, ctx: Prec, operator: &str, own: Prec, field: &str) -> String {
    let Some(operand) = node.child_by_field_name(field) else {
        return fallback(node, source);
    };
    let separator = if own == Prec::Factor { "" } else { " " };
    let body = format!("{operator}{separator}{}", expr(operand, source, own));
    parenthesize(body, ctx > own)
}

fn binary(node: Node, source: &str, ctx: Prec) -> String {
    let (Some(left), Some(right)) = (
        node.child_by_field_name("left"),
        node.child_by_field_name("right"),
    ) else {
        return fallback(node, source);
    };
    let operator = infix_operator(node, source);
    let Some(own) = binop_precedence(operator) else {
        return fallback(node, source);
    };
    // `**` is the only right-associative operator.
    let (left_prec, right_prec) = if operator == "**" {
        (own.next(), own)
    } else {
        (own, own.next())
    };
    let body = format!(
        "{} {operator} {}",
        expr(left, source, left_prec),
        expr(right, source, right_prec)
    );
    parenthesize(body, ctx > own)
}

fn binop_precedence(operator: &str) -> Option<Prec> {
    Some(match operator {
        "+" | "-" => Prec::Arith,
        "*" | "@" | "/" | "%" | "//" => Prec::Term,
        "<<" | ">>" => Prec::Shift,
        "|" => BOR,
        "^" => Prec::Bxor,
        "&" => Prec::Band,
        "**" => Prec::Power,
        _ => return None,
    })
}

/// `ast.BoolOp` holds a flat value list, so chained same-operator nodes are
/// flattened here. Parenthesized operands stay nested, matching the AST the
/// parser would build.
fn boolean(node: Node, source: &str, ctx: Prec) -> String {
    let operator = infix_operator(node, source);
    let own = match operator {
        "and" => Prec::And,
        "or" => Prec::Or,
        _ => return fallback(node, source),
    };
    let mut values = Vec::new();
    flatten_boolean(node, source, operator, &mut values);
    if values.len() < 2 {
        return fallback(node, source);
    }
    let mut level = own;
    let rendered: Vec<String> = values
        .iter()
        .map(|value| {
            level = level.next();
            expr(*value, source, level)
        })
        .collect();
    parenthesize(rendered.join(&format!(" {operator} ")), ctx > own)
}

fn flatten_boolean<'a>(node: Node<'a>, source: &str, operator: &str, out: &mut Vec<Node<'a>>) {
    for field in ["left", "right"] {
        let Some(child) = node.child_by_field_name(field) else {
            continue;
        };
        if child.kind() == "boolean_operator" && infix_operator(child, source) == operator {
            flatten_boolean(child, source, operator, out);
        } else {
            out.push(child);
        }
    }
}

fn comparison(node: Node, source: &str, ctx: Prec) -> String {
    let operand_prec = Prec::Cmp.next();
    let mut body = String::new();
    let mut cursor = node.walk();
    if cursor.goto_first_child() {
        loop {
            let child = cursor.node();
            if cursor.field_name() == Some("operators") {
                body.push_str(&format!(" {} ", comparison_operator(child, source)));
            } else if child.is_named() {
                body.push_str(&expr(child, source, operand_prec));
            }
            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }
    parenthesize(body, ctx > Prec::Cmp)
}

/// `is not` / `not in` arrive as one anonymous node holding two tokens;
/// rebuilding from the tokens normalizes any source spacing.
fn comparison_operator(node: Node, source: &str) -> String {
    let tokens: Vec<&str> = all_children(node)
        .iter()
        .map(|child| text(*child, source))
        .collect();
    if tokens.is_empty() {
        text(node, source).to_string()
    } else {
        tokens.join(" ")
    }
}

fn conditional(node: Node, source: &str, ctx: Prec) -> String {
    let parts = named_children(node);
    if parts.len() != 3 {
        return fallback(node, source);
    }
    let body = format!(
        "{} if {} else {}",
        expr(parts[0], source, Prec::Test.next()),
        expr(parts[1], source, Prec::Test.next()),
        expr(parts[2], source, Prec::Test)
    );
    parenthesize(body, ctx > Prec::Test)
}

fn lambda(node: Node, source: &str, ctx: Prec) -> String {
    let Some(body_node) = node.child_by_field_name("body") else {
        return fallback(node, source);
    };
    let params = node
        .child_by_field_name("parameters")
        .map(|params| unparse_parameters(params, source))
        .unwrap_or_default();
    let head = if params.is_empty() {
        "lambda".to_string()
    } else {
        format!("lambda {params}")
    };
    let body = format!("{head}: {}", expr(body_node, source, Prec::Test));
    parenthesize(body, ctx > Prec::Test)
}

/// `visit_Call`: positional arguments first, keyword arguments (including
/// `**kwargs`) afterwards, regardless of their source order.
fn call(node: Node, source: &str) -> String {
    let (Some(function), Some(arguments)) = (
        node.child_by_field_name("function"),
        node.child_by_field_name("arguments"),
    ) else {
        return fallback(node, source);
    };
    let mut positional = Vec::new();
    let mut keywords = Vec::new();
    for child in named_children(arguments) {
        match child.kind() {
            "keyword_argument" | "dictionary_splat" => keywords.push(child),
            "comment" => {}
            _ => positional.push(child),
        }
    }
    let rendered: Vec<String> = positional
        .into_iter()
        .chain(keywords)
        .map(|child| expr(child, source, Prec::Test))
        .collect();
    format!(
        "{}({})",
        expr(function, source, Prec::Atom),
        rendered.join(", ")
    )
}

fn attribute(node: Node, source: &str) -> String {
    let (Some(object), Some(name)) = (
        node.child_by_field_name("object"),
        node.child_by_field_name("attribute"),
    ) else {
        return fallback(node, source);
    };
    // `3 .bit_length` — an integer (or bool, an int subclass) needs the space
    // so the dot is not read as a decimal point.
    let spacer = match unwrap_transparent(object).kind() {
        "integer" => {
            if text(unwrap_transparent(object), source).ends_with(['j', 'J']) {
                ""
            } else {
                " "
            }
        }
        "true" | "false" => " ",
        _ => "",
    };
    format!(
        "{}{spacer}.{}",
        expr(object, source, Prec::Atom),
        text(name, source)
    )
}

fn subscript(node: Node, source: &str) -> String {
    let Some(value) = node.child_by_field_name("value") else {
        return fallback(node, source);
    };
    let indices = field_nodes(node, "subscript");
    // A non-empty tuple index drops its parentheses; `d[c,]` stays a
    // one-element tuple and keeps the trailing comma.
    let is_tuple = indices.len() > 1 || (indices.len() == 1 && has_anonymous_child(node, ","));
    let rendered = if is_tuple {
        items_view(&indices, source)
    } else if indices.len() == 1 {
        expr(indices[0], source, Prec::Test)
    } else {
        String::new()
    };
    format!("{}[{rendered}]", expr(value, source, Prec::Atom))
}

/// `generic_type`/`type_parameter`: the annotation spelling of a subscript.
/// Multiple parameters form the AST's non-empty tuple slice, which renders
/// without parentheses.
fn generic_type(node: Node, source: &str) -> String {
    let children = named_children(node);
    let Some(value) = children.first() else {
        return fallback(node, source);
    };
    let parameters = children
        .iter()
        .find(|child| child.kind() == "type_parameter");
    let rendered = match parameters {
        Some(parameters) => {
            let items = named_children(*parameters);
            if items.len() > 1 || (items.len() == 1 && has_anonymous_child(*parameters, ",")) {
                items_view(&items, source)
            } else if items.len() == 1 {
                expr(items[0], source, Prec::Test)
            } else {
                String::new()
            }
        }
        None => return fallback(node, source),
    };
    format!("{}[{rendered}]", expr(*value, source, Prec::Atom))
}

/// Flatten an annotation union into its `|` operands and render them as the
/// left-associative `BitOr` chain the AST holds: every operand except the
/// last sits on the left spine, the last one is a right operand.
fn union_type(node: Node, source: &str, ctx: Prec) -> String {
    let mut members = Vec::new();
    flatten_union(node, source, &mut members);
    if members.len() < 2 {
        return fallback(node, source);
    }
    let last = members.len() - 1;
    let rendered: Vec<String> = members
        .iter()
        .enumerate()
        .map(|(index, member)| {
            let prec = if index == last { BOR.next() } else { BOR };
            expr(*member, source, prec)
        })
        .collect();
    parenthesize(rendered.join(" | "), ctx > BOR)
}

/// Parenthesized members stay intact: the AST nests them on the right of the
/// chain, where `ast.unparse` re-adds the parentheses.
fn flatten_union<'a>(node: Node<'a>, source: &str, out: &mut Vec<Node<'a>>) {
    match node.kind() {
        "union_type" => {
            for child in named_children(node) {
                flatten_union(child, source, out);
            }
        }
        "type" => match named_children(node).first() {
            Some(inner) => flatten_union(*inner, source, out),
            None => out.push(node),
        },
        "binary_operator" if infix_operator(node, source) == "|" => {
            match (
                node.child_by_field_name("left"),
                node.child_by_field_name("right"),
            ) {
                (Some(left), Some(right)) => {
                    flatten_union(left, source, out);
                    flatten_union(right, source, out);
                }
                _ => out.push(node),
            }
        }
        _ => out.push(node),
    }
}

fn slice(node: Node, source: &str) -> String {
    let mut lower = None;
    let mut upper = None;
    let mut step = None;
    let mut colons = 0usize;
    for child in all_children(node) {
        if !child.is_named() && child.kind() == ":" {
            colons += 1;
            continue;
        }
        if !child.is_named() {
            continue;
        }
        match colons {
            0 => lower = Some(child),
            1 => upper = Some(child),
            _ => step = Some(child),
        }
    }
    let mut out = String::new();
    if let Some(lower) = lower {
        out.push_str(&expr(lower, source, Prec::Test));
    }
    out.push(':');
    if let Some(upper) = upper {
        out.push_str(&expr(upper, source, Prec::Test));
    }
    if let Some(step) = step {
        out.push(':');
        out.push_str(&expr(step, source, Prec::Test));
    }
    out
}

fn tuple(node: Node, source: &str, ctx: Prec) -> String {
    let elements = named_children(node);
    let wrap = elements.is_empty() || ctx > Prec::Tuple;
    parenthesize(items_view(&elements, source), wrap)
}

fn dictionary(node: Node, source: &str) -> String {
    let items: Vec<String> = named_children(node)
        .iter()
        .map(|child| expr(*child, source, Prec::Test))
        .collect();
    format!("{{{}}}", items.join(", "))
}

fn pair(node: Node, source: &str) -> String {
    let (Some(key), Some(value)) = (
        node.child_by_field_name("key"),
        node.child_by_field_name("value"),
    ) else {
        return fallback(node, source);
    };
    format!(
        "{}: {}",
        expr(key, source, Prec::Test),
        expr(value, source, Prec::Test)
    )
}

fn splat(node: Node, source: &str, marker: &str) -> String {
    match named_children(node).first() {
        Some(inner) => format!("{marker}{}", expr(*inner, source, Prec::Expr)),
        None => marker.to_string(),
    }
}

fn comprehension(node: Node, source: &str, open: &str, close: &str) -> String {
    let Some(body) = node.child_by_field_name("body") else {
        return fallback(node, source);
    };
    let head = if body.kind() == "pair" {
        pair(body, source)
    } else {
        expr(body, source, Prec::Test)
    };
    let mut out = String::from(open);
    out.push_str(&head);
    for child in named_children(node) {
        match child.kind() {
            "for_in_clause" => out.push_str(&for_in_clause(child, source)),
            "if_clause" => {
                let conditions = named_children(child);
                for condition in conditions {
                    out.push_str(&format!(
                        " if {}",
                        expr(condition, source, Prec::Test.next())
                    ));
                }
            }
            _ => {}
        }
    }
    out.push_str(close);
    out
}

fn for_in_clause(node: Node, source: &str) -> String {
    let targets = field_nodes(node, "left");
    let iterables = field_nodes(node, "right");
    let keyword = if has_anonymous_child(node, "async") {
        " async for "
    } else {
        " for "
    };
    let target = targets
        .iter()
        .map(|child| expr(*child, source, Prec::Tuple))
        .collect::<Vec<_>>()
        .join(", ");
    let iterable = iterables
        .iter()
        .map(|child| expr(*child, source, Prec::Test.next()))
        .collect::<Vec<_>>()
        .join(", ");
    format!("{keyword}{target} in {iterable}")
}

fn await_expr(node: Node, source: &str, ctx: Prec) -> String {
    let body = match named_children(node).first() {
        Some(inner) => format!("await {}", expr(*inner, source, Prec::Atom)),
        None => "await".to_string(),
    };
    parenthesize(body, ctx > Prec::Await)
}

fn yield_expr(node: Node, source: &str, ctx: Prec) -> String {
    let value = named_children(node).into_iter().next();
    let from = has_anonymous_child(node, "from");
    let body = match (value, from) {
        (Some(inner), true) => format!("yield from {}", expr(inner, source, Prec::Atom)),
        (Some(inner), false) => format!("yield {}", expr(inner, source, Prec::Atom)),
        (None, _) => "yield".to_string(),
    };
    parenthesize(body, ctx > Prec::Yield)
}

fn keyword_argument(node: Node, source: &str) -> String {
    let (Some(name), Some(value)) = (
        node.child_by_field_name("name"),
        node.child_by_field_name("value"),
    ) else {
        return fallback(node, source);
    };
    format!("{}={}", text(name, source), expr(value, source, Prec::Test))
}

fn named_expression(node: Node, source: &str, ctx: Prec) -> String {
    let (Some(name), Some(value)) = (
        node.child_by_field_name("name"),
        node.child_by_field_name("value"),
    ) else {
        return fallback(node, source);
    };
    let body = format!(
        "{} := {}",
        expr(name, source, Prec::Atom),
        expr(value, source, Prec::Atom)
    );
    parenthesize(body, ctx > Prec::NamedExpr)
}

// ---------------------------------------------------------------------------
// Parameter lists (`visit_arguments`)
// ---------------------------------------------------------------------------

struct Param<'a> {
    name: String,
    annotation: Option<Node<'a>>,
    default: Option<Node<'a>>,
}

impl<'a> Param<'a> {
    fn render(&self, source: &str) -> String {
        let mut out = self.name.clone();
        if let Some(annotation) = self.annotation {
            out.push_str(": ");
            out.push_str(&expr(annotation, source, Prec::Test));
        }
        if let Some(default) = self.default {
            out.push('=');
            out.push_str(&expr(default, source, Prec::Test));
        }
        out
    }
}

#[derive(Default)]
struct Arguments<'a> {
    posonly: Vec<Param<'a>>,
    args: Vec<Param<'a>>,
    vararg: Option<Param<'a>>,
    bare_star: bool,
    kwonly: Vec<Param<'a>>,
    kwarg: Option<Param<'a>>,
}

/// Render a `parameters`/`lambda_parameters` node the way
/// `ast.unparse(node.args)` would — without the enclosing parentheses.
pub fn unparse_parameters(node: Node, source: &str) -> String {
    let arguments = collect_arguments(node, source);
    let mut out = String::new();
    let mut first = true;

    let positional: Vec<&Param> = arguments
        .posonly
        .iter()
        .chain(arguments.args.iter())
        .collect();
    for (index, param) in positional.iter().enumerate() {
        if first {
            first = false;
        } else {
            out.push_str(", ");
        }
        out.push_str(&param.render(source));
        if index + 1 == arguments.posonly.len() {
            out.push_str(", /");
        }
    }

    if arguments.vararg.is_some() || !arguments.kwonly.is_empty() || arguments.bare_star {
        if first {
            first = false;
        } else {
            out.push_str(", ");
        }
        out.push('*');
        if let Some(vararg) = &arguments.vararg {
            out.push_str(&vararg.render(source));
        }
    }

    for param in &arguments.kwonly {
        out.push_str(", ");
        out.push_str(&param.render(source));
    }

    if let Some(kwarg) = &arguments.kwarg {
        if !first {
            out.push_str(", ");
        }
        out.push_str("**");
        out.push_str(&kwarg.render(source));
    }

    out
}

fn collect_arguments<'a>(node: Node<'a>, source: &str) -> Arguments<'a> {
    let mut arguments = Arguments::default();
    let mut in_kwonly = false;
    for child in named_children(node) {
        match child.kind() {
            "positional_separator" => {
                arguments.posonly = std::mem::take(&mut arguments.args);
            }
            "keyword_separator" => {
                arguments.bare_star = true;
                in_kwonly = true;
            }
            "list_splat_pattern" => {
                arguments.vararg = Some(splat_param(child, source, None));
                in_kwonly = true;
            }
            "dictionary_splat_pattern" => {
                arguments.kwarg = Some(splat_param(child, source, None));
            }
            "typed_parameter" => {
                let annotation = child.child_by_field_name("type");
                match named_children(child).first().copied() {
                    Some(inner) if inner.kind() == "list_splat_pattern" => {
                        arguments.vararg = Some(splat_param(inner, source, annotation));
                        in_kwonly = true;
                    }
                    Some(inner) if inner.kind() == "dictionary_splat_pattern" => {
                        arguments.kwarg = Some(splat_param(inner, source, annotation));
                    }
                    Some(inner) => push_param(
                        &mut arguments,
                        in_kwonly,
                        Param {
                            name: text(inner, source).to_string(),
                            annotation,
                            default: None,
                        },
                    ),
                    None => {}
                }
            }
            "default_parameter" | "typed_default_parameter" => {
                let Some(name) = child.child_by_field_name("name") else {
                    continue;
                };
                push_param(
                    &mut arguments,
                    in_kwonly,
                    Param {
                        name: text(name, source).to_string(),
                        annotation: child.child_by_field_name("type"),
                        default: child.child_by_field_name("value"),
                    },
                );
            }
            "identifier" => push_param(
                &mut arguments,
                in_kwonly,
                Param {
                    name: text(child, source).to_string(),
                    annotation: None,
                    default: None,
                },
            ),
            _ => {}
        }
    }
    arguments
}

fn push_param<'a>(arguments: &mut Arguments<'a>, in_kwonly: bool, param: Param<'a>) {
    if in_kwonly {
        arguments.kwonly.push(param);
    } else {
        arguments.args.push(param);
    }
}

fn splat_param<'a>(node: Node<'a>, source: &str, annotation: Option<Node<'a>>) -> Param<'a> {
    let name = named_children(node)
        .first()
        .map(|inner| text(*inner, source).to_string())
        .unwrap_or_default();
    Param {
        name,
        annotation,
        default: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tree_sitter::Parser;

    /// Render `def f(<params>)` the way the oracle stores `symbols.args`.
    fn args_of(source: &str) -> String {
        let mut parser = Parser::new();
        parser
            .set_language(&tree_sitter_python::LANGUAGE.into())
            .unwrap();
        let tree = parser.parse(source.as_bytes(), None).unwrap();
        let mut cursor = tree.root_node().walk();
        let definition = tree
            .root_node()
            .named_children(&mut cursor)
            .find(|node| node.kind() == "function_definition")
            .expect("probe source defines a function");
        let params = definition.child_by_field_name("parameters").unwrap();
        format!("({})", unparse_parameters(params, source))
    }

    /// Golden vectors captured from `ast.unparse(node.args)` under the pinned
    /// oracle interpreter (Python 3.12.9, 2026-08-17).
    #[test]
    fn matches_ast_unparse_golden_vectors() {
        for (source, expected) in [
            ("def f(): pass", "()"),
            ("def f(a, b, c): pass", "(a, b, c)"),
            (
                "def f(a=1, b=None, c=True, d=False): pass",
                "(a=1, b=None, c=True, d=False)",
            ),
            ("def f(a: int, b: str='x'): pass", "(a: int, b: str='x')"),
            ("def f(a, /, b, *, c): pass", "(a, /, b, *, c)"),
            (
                "def f(a, /, b=2, *args, c, d=4, **kw): pass",
                "(a, /, b=2, *args, c, d=4, **kw)",
            ),
            ("def f(*args): pass", "(*args)"),
            ("def f(**kw): pass", "(**kw)"),
            ("def f(*, a): pass", "(*, a)"),
            (
                "def f(*args: int, **kw: str): pass",
                "(*args: int, **kw: str)",
            ),
            (
                "def f(a: Optional[Path]=None): pass",
                "(a: Optional[Path]=None)",
            ),
            ("def f(a: list[dict]=[]): pass", "(a: list[dict]=[])"),
            ("def f(a: Path | None=None): pass", "(a: Path | None=None)"),
            (
                "def f(a: Dict[str, List[int]]): pass",
                "(a: Dict[str, List[int]])",
            ),
            ("def f(a=(1, 2)): pass", "(a=(1, 2))"),
            ("def f(a=()): pass", "(a=())"),
            ("def f(a=(1,)): pass", "(a=(1,))"),
            ("def f(a=[]): pass", "(a=[])"),
            ("def f(a={}): pass", "(a={})"),
            ("def f(a={'k': 1, 'j': 2}): pass", "(a={'k': 1, 'j': 2})"),
            ("def f(a={1, 2}): pass", "(a={1, 2})"),
            ("def f(a=-1): pass", "(a=-1)"),
            ("def f(a=not x): pass", "(a=not x)"),
            ("def f(a=~x): pass", "(a=~x)"),
            ("def f(a=+1): pass", "(a=+1)"),
            ("def f(a=1+2*3): pass", "(a=1 + 2 * 3)"),
            ("def f(a=(1+2)*3): pass", "(a=(1 + 2) * 3)"),
            ("def f(a=1<<2): pass", "(a=1 << 2)"),
            ("def f(a=2**3**4): pass", "(a=2 ** 3 ** 4)"),
            ("def f(a=(2**3)**4): pass", "(a=(2 ** 3) ** 4)"),
            ("def f(a=-2**3): pass", "(a=-2 ** 3)"),
            ("def f(a=(-2)**3): pass", "(a=(-2) ** 3)"),
            ("def f(a=1/2//3%4): pass", "(a=1 / 2 // 3 % 4)"),
            ("def f(a=x if y else z): pass", "(a=x if y else z)"),
            (
                "def f(a=1 if 2 else 3 if 4 else 5): pass",
                "(a=1 if 2 else 3 if 4 else 5)",
            ),
            (
                "def f(a=(1 if 2 else 3) if 4 else 5): pass",
                "(a=(1 if 2 else 3) if 4 else 5)",
            ),
            ("def f(a=lambda x: x): pass", "(a=lambda x: x)"),
            (
                "def f(a=lambda x, *, y=1: x+y): pass",
                "(a=lambda x, *, y=1: x + y)",
            ),
            ("def f(a=lambda: 0): pass", "(a=lambda: 0)"),
            ("def f(a=x and y or z): pass", "(a=x and y or z)"),
            ("def f(a=(x or y) and z): pass", "(a=(x or y) and z)"),
            ("def f(a=x and y and z): pass", "(a=x and y and z)"),
            ("def f(a=x and (y and z)): pass", "(a=x and (y and z))"),
            ("def f(a=x or y and z): pass", "(a=x or (y and z))"),
            (
                "def f(a=a and b or c and d): pass",
                "(a=a and b or (c and d))",
            ),
            ("def f(a=x < y <= z): pass", "(a=x < y <= z)"),
            ("def f(a=(x < y) <= z): pass", "(a=(x < y) <= z)"),
            ("def f(a=x is not None): pass", "(a=x is not None)"),
            ("def f(a=x not in y): pass", "(a=x not in y)"),
            (
                "def f(a=sqlite3.Connection): pass",
                "(a=sqlite3.Connection)",
            ),
            ("def f(a=mod.sub.attr): pass", "(a=mod.sub.attr)"),
            ("def f(a=globals()): pass", "(a=globals())"),
            (
                "def f(a=call(1, k=2, *b, **c)): pass",
                "(a=call(1, *b, k=2, **c))",
            ),
            ("def f(a=d['k']): pass", "(a=d['k'])"),
            ("def f(a=d[1:2]): pass", "(a=d[1:2])"),
            ("def f(a=d[1:2:3]): pass", "(a=d[1:2:3])"),
            ("def f(a=d[:]): pass", "(a=d[:])"),
            ("def f(a=d[1:]): pass", "(a=d[1:])"),
            ("def f(a=d[::2]): pass", "(a=d[::2])"),
            ("def f(a=d[b, c]): pass", "(a=d[b, c])"),
            ("def f(a=d[c,]): pass", "(a=d[c,])"),
            ("def f(a=d[...]): pass", "(a=d[...])"),
            ("def f(a='single'): pass", "(a='single')"),
            ("def f(a=\"double\"): pass", "(a='double')"),
            ("def f(a='has \"dq\"'): pass", "(a='has \"dq\"')"),
            ("def f(a='unié'): pass", "(a='unié')"),
            ("def f(a=b'bytes'): pass", "(a=b'bytes')"),
            ("def f(a=rb'raw'): pass", "(a=b'raw')"),
            ("def f(a=0x10): pass", "(a=16)"),
            ("def f(a=0o17): pass", "(a=15)"),
            ("def f(a=0b101): pass", "(a=5)"),
            ("def f(a=1_000_000): pass", "(a=1000000)"),
            ("def f(a=1e3): pass", "(a=1000.0)"),
            ("def f(a=1E-3): pass", "(a=0.001)"),
            ("def f(a=.5): pass", "(a=0.5)"),
            ("def f(a=5.): pass", "(a=5.0)"),
            ("def f(a=1_0.5_0): pass", "(a=10.5)"),
            ("def f(a=3j): pass", "(a=3j)"),
            ("def f(a=1+2j): pass", "(a=1 + 2j)"),
            ("def f(a=float('inf')): pass", "(a=float('inf'))"),
            ("def f(a=...): pass", "(a=...)"),
            ("def f(a=None): pass", "(a=None)"),
            ("def f(a='implicit' 'concat'): pass", "(a='implicitconcat')"),
            ("def f(a=x[0].y(1).z): pass", "(a=x[0].y(1).z)"),
            ("def f(a=(yield)): pass", "(a=(yield))"),
            ("def f(a=(yield from g)): pass", "(a=(yield from g))"),
            ("def f(a=[i for i in r]): pass", "(a=[i for i in r])"),
            (
                "def f(a={k: v for k, v in r}): pass",
                "(a={k: v for k, v in r})",
            ),
            ("def f(a={i for i in r}): pass", "(a={i for i in r})"),
            ("def f(a=(i for i in r)): pass", "(a=(i for i in r))"),
            (
                "def f(a=[i for i in r if i]): pass",
                "(a=[i for i in r if i])",
            ),
            ("def f(a=[i for k, v in r]): pass", "(a=[i for k, v in r])"),
            (
                "def f(a=[i async for i in r]): pass",
                "(a=[i async for i in r])",
            ),
            ("async def f(a=await g()): pass", "(a=await g())"),
            ("def f(a=(*b,)): pass", "(a=(*b,))"),
            ("def f(a={**b, 'k': 1}): pass", "(a={**b, 'k': 1})"),
            ("def f(a=g(*b, **c)): pass", "(a=g(*b, **c))"),
            ("def f(a=not not x): pass", "(a=not not x)"),
            ("def f(a=--x): pass", "(a=--x)"),
            ("def f(a=-(-x)): pass", "(a=--x)"),
            ("def f(a=(3).__abs__): pass", "(a=3 .__abs__)"),
            ("def f(a=(x := 1)): pass", "(a=(x := 1))"),
            ("def f(a: 'Forward'): pass", "(a: 'Forward')"),
            ("def f(a: \"Quoted\"='x'): pass", "(a: 'Quoted'='x')"),
            (
                "def f(self, x: int=5, *, y: 'T'=None, **kw: Any) -> None: pass",
                "(self, x: int=5, *, y: 'T'=None, **kw: Any)",
            ),
            ("def f(a=f'v={x}'): pass", "(a=f'v={x}')"),
            ("def f(a=f\"v={x}\"): pass", "(a=f'v={x}')"),
            ("def f(a=f'{ x + 1 }'): pass", "(a=f'{x + 1}')"),
            ("def f(a=f'{x!r}'): pass", "(a=f'{x!r}')"),
            ("def f(a=f'{x:>{w}}'): pass", "(a=f'{x:>{w}}')"),
            // Subscripted annotations parse as `generic_type`; a forward
            // reference written with double quotes must still be re-quoted
            // the way `repr` would.
            (
                "def f(a: Optional[\"PackageFinder\"]=None): pass",
                "(a: Optional['PackageFinder']=None)",
            ),
            ("def f(a: list[\"Foo\"]): pass", "(a: list['Foo'])"),
            (
                "def f(a: Literal[\"x\", 'y']): pass",
                "(a: Literal['x', 'y'])",
            ),
            (
                "def f(a: Optional[Dict[str, \"T\"]]=None): pass",
                "(a: Optional[Dict[str, 'T']]=None)",
            ),
            (
                "def f(a: Callable[..., int]): pass",
                "(a: Callable[..., int])",
            ),
            ("def f(a: tuple[int, ...]): pass", "(a: tuple[int, ...])"),
            ("def f(a: np.ndarray): pass", "(a: np.ndarray)"),
            (
                "def f(a: Annotated[int, Field(gt=0)]): pass",
                "(a: Annotated[int, Field(gt=0)])",
            ),
            ("def f(a: \"Foo\"=None): pass", "(a: 'Foo'=None)"),
            ("def f(a: dict[str,int]): pass", "(a: dict[str, int])"),
            // `union_type` mixes with plain `|` operators once a member is
            // subscripted; the chain must stay flat.
            (
                "def f(a: Callable[[R], None] | _T | None | _U[_D]=None): pass",
                "(a: Callable[[R], None] | _T | None | _U[_D]=None)",
            ),
            ("def f(a: list[int] | None): pass", "(a: list[int] | None)"),
            ("def f(a: int | str | None): pass", "(a: int | str | None)"),
            (
                "def f(a: list[int] | (str | bytes)): pass",
                "(a: list[int] | (str | bytes))",
            ),
            (
                "def f(a: dict[str, int] | list[int] | None): pass",
                "(a: dict[str, int] | list[int] | None)",
            ),
        ] {
            assert_eq!(args_of(source), expected, "source: {source}");
        }
    }
}
