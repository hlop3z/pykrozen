//! Markdown report generator for performance test results.
//!
//! Generates GitHub-flavored Markdown reports with performance metrics.

use std::fs::File;
use std::io::{self, Write};
use std::path::Path;
use std::time::Duration;

use chrono::{DateTime, Local};

use crate::stats::StressTestResult;

/// A single test result entry for the report.
#[derive(Debug, Clone)]
pub struct TestEntry {
    /// Name of the test
    pub name: String,
    /// Test type (http-get, http-post, websocket, mixed)
    pub test_type: String,
    /// Number of concurrent workers
    pub concurrency: usize,
    /// Test duration
    pub duration: Duration,
    /// The stress test result
    pub result: StressTestResultData,
}

/// Cloneable version of StressTestResult for storage.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct StressTestResultData {
    pub duration: Duration,
    pub total_requests: u64,
    pub successful_requests: u64,
    pub failed_requests: u64,
    pub rps: f64,
    pub avg_latency: Duration,
    pub min_latency: Duration,
    pub max_latency: Duration,
    pub bytes_transferred: u64,
    pub throughput_mbps: f64,
}

impl From<&StressTestResult> for StressTestResultData {
    fn from(r: &StressTestResult) -> Self {
        Self {
            duration: r.duration,
            total_requests: r.total_requests,
            successful_requests: r.successful_requests,
            failed_requests: r.failed_requests,
            rps: r.rps,
            avg_latency: r.avg_latency,
            min_latency: r.min_latency,
            max_latency: r.max_latency,
            bytes_transferred: r.bytes_transferred,
            throughput_mbps: r.throughput_mbps,
        }
    }
}

/// Markdown report writer.
#[derive(Debug, Default)]
pub struct MarkdownReport {
    /// Test entries
    entries: Vec<TestEntry>,
    /// Peak RPS test results (concurrency -> result)
    peak_results: Vec<(usize, StressTestResultData)>,
    /// Report timestamp
    timestamp: Option<DateTime<Local>>,
    /// Server configuration
    server_host: String,
    server_http_port: u16,
    server_ws_port: u16,
}

impl MarkdownReport {
    /// Create a new report instance.
    pub fn new(host: &str, http_port: u16, ws_port: u16) -> Self {
        Self {
            entries: Vec::new(),
            peak_results: Vec::new(),
            timestamp: Some(Local::now()),
            server_host: host.to_string(),
            server_http_port: http_port,
            server_ws_port: ws_port,
        }
    }

    /// Add a test result to the report.
    pub fn add_result(
        &mut self,
        name: &str,
        test_type: &str,
        concurrency: usize,
        duration: Duration,
        result: &StressTestResult,
    ) {
        self.entries.push(TestEntry {
            name: name.to_string(),
            test_type: test_type.to_string(),
            concurrency,
            duration,
            result: result.into(),
        });
    }

    /// Add a peak RPS test result.
    #[allow(dead_code)]
    pub fn add_peak_result(&mut self, concurrency: usize, result: &StressTestResult) {
        self.peak_results.push((concurrency, result.into()));
    }

    /// Generate the markdown report content.
    pub fn generate(&self) -> String {
        let mut md = String::new();

        // Header
        md.push_str("# PyKrozen Performance Report\n\n");

        // Metadata
        if let Some(ts) = self.timestamp {
            md.push_str(&format!(
                "**Generated:** {}\n\n",
                ts.format("%Y-%m-%d %H:%M:%S")
            ));
        }

        md.push_str("## Server Configuration\n\n");
        md.push_str("| Setting | Value |\n");
        md.push_str("|---------|-------|\n");
        md.push_str(&format!("| Host | `{}` |\n", self.server_host));
        md.push_str(&format!("| HTTP Port | `{}` |\n", self.server_http_port));
        md.push_str(&format!("| WebSocket Port | `{}` |\n", self.server_ws_port));
        md.push_str("\n");

        // Summary table
        if !self.entries.is_empty() {
            md.push_str("## Test Results Summary\n\n");
            md.push_str("| Test | Type | Workers | Duration | Requests | RPS | Avg Latency | Success Rate |\n");
            md.push_str("|------|------|---------|----------|----------|-----|-------------|-------------|\n");

            for entry in &self.entries {
                let success_rate = if entry.result.total_requests > 0 {
                    (entry.result.successful_requests as f64 / entry.result.total_requests as f64)
                        * 100.0
                } else {
                    0.0
                };

                md.push_str(&format!(
                    "| {} | {} | {} | {:.1}s | {} | **{:.0}** | {:.2}ms | {:.1}% |\n",
                    entry.name,
                    entry.test_type,
                    entry.concurrency,
                    entry.duration.as_secs_f64(),
                    entry.result.total_requests,
                    entry.result.rps,
                    entry.result.avg_latency.as_secs_f64() * 1000.0,
                    success_rate,
                ));
            }
            md.push_str("\n");
        }

        // Detailed results
        if !self.entries.is_empty() {
            md.push_str("## Detailed Results\n\n");

            for entry in &self.entries {
                md.push_str(&format!("### {}\n\n", entry.name));

                md.push_str("#### Performance Metrics\n\n");
                md.push_str("| Metric | Value |\n");
                md.push_str("|--------|-------|\n");
                md.push_str(&format!(
                    "| Total Requests | {} |\n",
                    entry.result.total_requests
                ));
                md.push_str(&format!(
                    "| Successful | {} ({:.1}%) |\n",
                    entry.result.successful_requests,
                    if entry.result.total_requests > 0 {
                        (entry.result.successful_requests as f64
                            / entry.result.total_requests as f64)
                            * 100.0
                    } else {
                        0.0
                    }
                ));
                md.push_str(&format!("| Failed | {} |\n", entry.result.failed_requests));
                md.push_str(&format!(
                    "| **Requests/sec** | **{:.2}** |\n",
                    entry.result.rps
                ));
                md.push_str("\n");

                md.push_str("#### Latency\n\n");
                md.push_str("| Metric | Value |\n");
                md.push_str("|--------|-------|\n");
                md.push_str(&format!(
                    "| Min | {} |\n",
                    format_duration(entry.result.min_latency)
                ));
                md.push_str(&format!(
                    "| Avg | {} |\n",
                    format_duration(entry.result.avg_latency)
                ));
                md.push_str(&format!(
                    "| Max | {} |\n",
                    format_duration(entry.result.max_latency)
                ));
                md.push_str("\n");

                md.push_str("#### Throughput\n\n");
                md.push_str("| Metric | Value |\n");
                md.push_str("|--------|-------|\n");
                md.push_str(&format!(
                    "| Bytes Transferred | {} |\n",
                    format_bytes(entry.result.bytes_transferred)
                ));
                md.push_str(&format!(
                    "| Rate | {:.2} MB/s |\n",
                    entry.result.throughput_mbps
                ));
                md.push_str("\n---\n\n");
            }
        }

        // Peak RPS results
        if !self.peak_results.is_empty() {
            md.push_str("## Peak RPS Analysis\n\n");
            md.push_str("| Workers | RPS | Avg Latency | Errors |\n");
            md.push_str("|---------|-----|-------------|--------|\n");

            for (concurrency, result) in &self.peak_results {
                md.push_str(&format!(
                    "| {} | **{:.0}** | {:.2}ms | {} |\n",
                    concurrency,
                    result.rps,
                    result.avg_latency.as_secs_f64() * 1000.0,
                    result.failed_requests,
                ));
            }
            md.push_str("\n");
        }

        // Footer
        md.push_str("---\n\n");
        md.push_str("*Generated by PyKrozen Test Suite*\n");

        md
    }

    /// Write the report to a file.
    pub fn write_to_file<P: AsRef<Path>>(&self, path: P) -> io::Result<()> {
        let content = self.generate();
        let mut file = File::create(path)?;
        file.write_all(content.as_bytes())?;
        Ok(())
    }

    /// Write the report to the default file in performance/ folder.
    pub fn write_default(&self) -> io::Result<String> {
        // Create performance directory if it doesn't exist
        let perf_dir = Path::new("performance");
        if !perf_dir.exists() {
            std::fs::create_dir_all(perf_dir)?;
        }

        let filename = format!(
            "performance/report_{}.md",
            Local::now().format("%Y%m%d_%H%M%S")
        );
        self.write_to_file(&filename)?;
        Ok(filename)
    }
}

/// Format a duration for display.
fn format_duration(d: Duration) -> String {
    let us = d.as_micros();
    if us < 1000 {
        format!("{}µs", us)
    } else if us < 1_000_000 {
        format!("{:.2}ms", us as f64 / 1000.0)
    } else {
        format!("{:.2}s", us as f64 / 1_000_000.0)
    }
}

/// Format bytes for display.
fn format_bytes(bytes: u64) -> String {
    if bytes < 1024 {
        format!("{} B", bytes)
    } else if bytes < 1024 * 1024 {
        format!("{:.2} KB", bytes as f64 / 1024.0)
    } else if bytes < 1024 * 1024 * 1024 {
        format!("{:.2} MB", bytes as f64 / 1024.0 / 1024.0)
    } else {
        format!("{:.2} GB", bytes as f64 / 1024.0 / 1024.0 / 1024.0)
    }
}
