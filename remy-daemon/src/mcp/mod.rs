//! `remy-daemon mcp` — the Rust host for the remy-index MCP read path (R4.1).
//! Per-session stdio transport, WAL read-only-style direct SQLite access
//! (INV-R2: no daemon involvement); the Python server at
//! remy-src/index_mcp_server.py is the byte-level rendering oracle.

mod common;
pub mod config;
mod facts;
mod freshness;
mod graph;
mod navigate;
mod search;

use std::process::ExitCode;
use std::sync::Arc;

use rmcp::handler::server::wrapper::Parameters;
use rmcp::model::{Implementation, ProtocolVersion, ServerCapabilities, ServerInfo};
use rmcp::schemars;
use rmcp::{tool, tool_handler, tool_router, ServerHandler, ServiceExt};
use serde::Deserialize;

use config::McpConfig;

const INSTRUCTIONS: &str = concat!(
    "Use this server to query code structure and call relationships in the indexed project.\n",
    "\n",
    "Prefer these tools over Read/Grep when your goal is to understand code:\n",
    "- To understand a function's purpose or signature: query_symbol_summary (instead of reading the source file)\n",
    "- To understand a file's overall role and key symbols: query_file_summary (instead of reading the whole file)\n",
    "- To find who calls a function or what it calls: query_callers / query_callees (instead of grep)\n",
    "- To assess which modules a file change would affect: query_impact (instead of manual search)\n",
    "- To locate where a symbol is defined: query_symbol (instead of glob/grep)\n",
    "- To search for a symbol when you don't know the exact name: query_search (fuzzy prefix/substring/typo)\n",
    "- To trace call paths between two or more symbols: query_flow (bidirectional BFS)\n",
    "- To get a subsystem-level overview: query_cluster_summary (cluster contracts, entry symbols)\n",
    "- To list a cluster's member files: query_cluster_files (optionally with short summaries)\n",
    "- To locate work by intent (\"where do I modify auth logic\"): query_navigate (LLM-ranked clusters/files)\n",
    "\n",
    "Index summaries are stored in English. Phrase query_search text and\n",
    "query_navigate intents in English for best lexical recall.\n",
    "\n",
    "Do NOT use these tools when:\n",
    "- You need to read file content before making an edit (use Read instead)\n",
    "- You are reading configuration files, templates, or non-code assets\n",
    "- The target file is not part of the project's code index",
);

fn default_depth() -> i64 {
    2
}
fn default_impact_depth() -> i64 {
    3
}
fn default_limit() -> i64 {
    10
}
fn default_match() -> String {
    "all".to_string()
}
fn default_flow_depth() -> i64 {
    15
}
fn default_flow_visited() -> i64 {
    2000
}
fn default_top_k() -> i64 {
    5
}

#[derive(Deserialize, schemars::JsonSchema)]
struct NameFileParams {
    name: String,
    #[serde(default)]
    file: String,
}

#[derive(Deserialize, schemars::JsonSchema)]
struct FileParams {
    file: String,
}

#[derive(Deserialize, schemars::JsonSchema)]
struct BfsParams {
    symbol: String,
    #[serde(default = "default_depth")]
    depth: i64,
    #[serde(default)]
    include_ambiguous: bool,
    #[serde(default)]
    static_only: bool,
}

#[derive(Deserialize, schemars::JsonSchema)]
struct ImpactParams {
    files: Vec<String>,
    #[serde(default = "default_impact_depth")]
    depth_up: i64,
    #[serde(default = "default_impact_depth")]
    depth_down: i64,
    #[serde(default)]
    include_ambiguous: bool,
    #[serde(default)]
    static_only: bool,
}

#[derive(Deserialize, schemars::JsonSchema)]
struct PatternsParams {
    #[serde(default)]
    pattern_type: String,
    #[serde(default)]
    signal_name: String,
    #[serde(default)]
    file: String,
}

#[derive(Deserialize, schemars::JsonSchema)]
struct SearchParams {
    text: String,
    #[serde(default = "default_limit")]
    limit: i64,
    #[serde(default)]
    file_hint: String,
    #[serde(rename = "match", default = "default_match")]
    match_mode: String,
    #[serde(default)]
    language: String,
    #[serde(default)]
    symbol_type: String,
    #[serde(default)]
    path_hint: String,
}

#[derive(Deserialize, schemars::JsonSchema)]
struct FlowParams {
    symbols: Vec<String>,
    #[serde(default = "default_flow_depth")]
    max_depth: i64,
    #[serde(default = "default_flow_visited")]
    max_visited: i64,
    #[serde(default)]
    static_only: bool,
}

#[derive(Deserialize, schemars::JsonSchema)]
struct ClusterSummaryParams {
    #[serde(default)]
    name: String,
}

#[derive(Deserialize, schemars::JsonSchema)]
struct ClusterFilesParams {
    cluster: String,
    #[serde(default)]
    with_summary: bool,
}

#[derive(Deserialize, schemars::JsonSchema)]
struct NavigateParams {
    intent: String,
    #[serde(default = "default_top_k")]
    top_k: i64,
}

#[derive(Clone)]
struct RemyIndexServer {
    cfg: Arc<McpConfig>,
    warning: Arc<String>,
}

fn opt(value: &str) -> Option<&str> {
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}

impl RemyIndexServer {
    fn wrap(&self, result: String) -> String {
        common::with_freshness(&self.warning, result)
    }
}

#[tool_router]
impl RemyIndexServer {
    #[tool(
        name = "query_symbol",
        description = "Find symbol definitions by name. Returns location, type, signature, layer, and summary for each match."
    )]
    async fn query_symbol(&self, Parameters(p): Parameters<NameFileParams>) -> String {
        self.wrap(facts::query_symbol_impl(&self.cfg, &p.name, opt(&p.file)))
    }

    #[tool(
        name = "query_symbol_summary",
        description = "Get symbol-level summary and docstring. Use for quick understanding of a function/class purpose."
    )]
    async fn query_symbol_summary(&self, Parameters(p): Parameters<NameFileParams>) -> String {
        self.wrap(facts::query_symbol_summary_impl(
            &self.cfg,
            &p.name,
            opt(&p.file),
        ))
    }

    #[tool(
        name = "query_file_summary",
        description = "Get file-level semantic summary: role, key symbols, layer, and status. Use for understanding a file's overall purpose before reading source."
    )]
    async fn query_file_summary(&self, Parameters(p): Parameters<FileParams>) -> String {
        self.wrap(facts::query_file_summary_impl(&self.cfg, &p.file))
    }

    #[tool(
        name = "query_callers",
        description = "Find upstream callers of a symbol via BFS. Returns callers grouped by depth level."
    )]
    async fn query_callers(&self, Parameters(p): Parameters<BfsParams>) -> String {
        self.wrap(graph::query_callers_impl(
            &self.cfg,
            &p.symbol,
            p.depth,
            p.include_ambiguous,
            p.static_only,
        ))
    }

    #[tool(
        name = "query_callees",
        description = "Find downstream callees of a symbol via BFS. Returns callees grouped by depth level."
    )]
    async fn query_callees(&self, Parameters(p): Parameters<BfsParams>) -> String {
        self.wrap(graph::query_callees_impl(
            &self.cfg,
            &p.symbol,
            p.depth,
            p.include_ambiguous,
            p.static_only,
        ))
    }

    #[tool(
        name = "query_impact",
        description = "Analyze impact radius for files. Shows upstream callers and downstream callees."
    )]
    async fn query_impact(&self, Parameters(p): Parameters<ImpactParams>) -> String {
        self.wrap(graph::query_impact_impl(
            &self.cfg,
            &p.files,
            p.depth_up,
            p.depth_down,
            p.include_ambiguous,
            p.static_only,
        ))
    }

    #[tool(
        name = "query_patterns",
        description = "Query event/callback registration patterns (Django signals, PyQt signals, observer pattern)."
    )]
    async fn query_patterns(&self, Parameters(p): Parameters<PatternsParams>) -> String {
        self.wrap(facts::query_patterns_impl(
            &self.cfg,
            opt(&p.pattern_type),
            opt(&p.signal_name),
            opt(&p.file),
        ))
    }

    #[tool(
        name = "query_search",
        description = "Search symbols with all/any/phrase matching and structural filters. Summaries are indexed in English; English query text maximizes lexical recall."
    )]
    async fn query_search(&self, Parameters(p): Parameters<SearchParams>) -> String {
        self.wrap(search::query_search_impl(
            &self.cfg,
            &p.text,
            p.limit,
            &p.file_hint,
            &p.match_mode,
            &p.language,
            &p.symbol_type,
            &p.path_hint,
        ))
    }

    #[tool(
        name = "query_flow",
        description = "Find call paths among named symbols via bidirectional BFS. Supports qualified syntax: bare name, file/path:name, or Class.method."
    )]
    async fn query_flow(&self, Parameters(p): Parameters<FlowParams>) -> String {
        self.wrap(graph::query_flow_impl(
            &self.cfg,
            &p.symbols,
            p.max_depth,
            p.max_visited,
            p.static_only,
        ))
    }

    #[tool(
        name = "query_cluster_summary",
        description = "Return subsystem-level summaries for one or all clusters: name, label, short/full descriptions, entry symbols, and file count."
    )]
    async fn query_cluster_summary(
        &self,
        Parameters(p): Parameters<ClusterSummaryParams>,
    ) -> String {
        self.wrap(facts::query_cluster_summary_impl(&self.cfg, opt(&p.name)))
    }

    #[tool(
        name = "query_cluster_files",
        description = "List member files of a cluster (path + layer). Set with_summary=True to append short file summaries inline."
    )]
    async fn query_cluster_files(&self, Parameters(p): Parameters<ClusterFilesParams>) -> String {
        self.wrap(facts::query_cluster_files_impl(
            &self.cfg,
            &p.cluster,
            p.with_summary,
        ))
    }

    #[tool(
        name = "query_navigate",
        description = "Locate work by natural-language intent over bounded cluster/file/symbol candidates. Returns top_k ranked entries with {cluster, file?, symbol?, relevance_score, rationale}. Index summaries are English; phrase the intent in English for lexical candidate recall (non-English intents fall back to cluster-level ranking)."
    )]
    async fn query_navigate(&self, Parameters(p): Parameters<NavigateParams>) -> String {
        self.wrap(navigate::query_navigate_impl(&self.cfg, &p.intent, p.top_k).await)
    }
}

#[tool_handler]
impl ServerHandler for RemyIndexServer {
    fn get_info(&self) -> ServerInfo {
        let mut info = ServerInfo::new(ServerCapabilities::builder().enable_tools().build());
        info.protocol_version = ProtocolVersion::default();
        let mut implementation = Implementation::from_build_env();
        implementation.name = "remy-index".to_string();
        implementation.title = None;
        implementation.version = env!("CARGO_PKG_VERSION").to_string();
        info.server_info = implementation;
        info.instructions = Some(INSTRUCTIONS.to_string());
        info
    }
}

pub fn run() -> ExitCode {
    let cfg = config::load();
    cfg.emit_diagnostics();
    if !cfg.server_enabled {
        eprintln!("remy-index MCP server disabled (REMY_MCP_SERVER_ENABLED=false)");
        return ExitCode::SUCCESS;
    }
    let warning = freshness::init_freshness(&cfg);
    let server = RemyIndexServer {
        cfg: Arc::new(cfg),
        warning: Arc::new(warning),
    };

    let runtime = match tokio::runtime::Runtime::new() {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("remy-daemon mcp: runtime start failed: {error}");
            return ExitCode::from(2);
        }
    };
    let outcome: Result<(), String> = runtime.block_on(async {
        let service = server
            .serve(rmcp::transport::io::stdio())
            .await
            .map_err(|error| error.to_string())?;
        service.waiting().await.map_err(|error| error.to_string())?;
        Ok(())
    });
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("remy-daemon mcp: {error}");
            ExitCode::from(2)
        }
    }
}
