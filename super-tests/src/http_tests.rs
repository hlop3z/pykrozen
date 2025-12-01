//! HTTP endpoint testing module.

use colored::Colorize;
use reqwest::Client;
use serde_json::{json, Value};

use crate::config::Config;
use crate::stats::Timer;

/// Test result for a single endpoint.
#[derive(Debug)]
pub struct EndpointTestResult {
    pub path: String,
    pub method: String,
    pub status: u16,
    pub latency_ms: f64,
    pub body_size: usize,
    pub success: bool,
    pub error: Option<String>,
}

impl EndpointTestResult {
    fn print(&self) {
        let status_str = if self.success {
            format!("{}", self.status).green()
        } else {
            format!("{}", self.status).red()
        };

        let result_icon = if self.success {
            "✓".green()
        } else {
            "✗".red()
        };

        println!(
            "  {} {} {} {} - {}ms, {} bytes",
            result_icon,
            self.method.blue(),
            self.path.white(),
            status_str,
            format!("{:.2}", self.latency_ms).yellow(),
            self.body_size
        );

        if let Some(ref err) = self.error {
            println!("      {} {}", "Error:".red(), err);
        }
    }
}

/// Run GET endpoint tests.
pub async fn run_get_tests(config: &Config, paths: &[String]) {
    let client = Client::new();
    let base_url = config.http_url();

    println!(
        "  Testing {} GET endpoint(s)...\n",
        paths.len().to_string().cyan()
    );

    let mut results = Vec::new();

    for path in paths {
        let result = test_get_endpoint(&client, &base_url, path).await;
        result.print();
        results.push(result);
    }

    // Additional standard tests
    println!("\n  {} Standard GET Tests:\n", "→".blue());

    // Test with query parameters
    let query_result = test_get_with_query(&client, &base_url, "/", &[("foo", "bar"), ("baz", "123")]).await;
    query_result.print();
    results.push(query_result);

    // Test 404
    let not_found_result = test_get_endpoint(&client, &base_url, "/this-path-should-not-exist-12345").await;
    not_found_result.print();
    results.push(not_found_result);

    print_summary(&results);
}

/// Run POST endpoint tests.
pub async fn run_post_tests(config: &Config, paths: &[String]) {
    let client = Client::new();
    let base_url = config.http_url();

    println!(
        "  Testing {} POST endpoint(s)...\n",
        paths.len().to_string().cyan()
    );

    let mut results = Vec::new();

    for path in paths {
        // Test with JSON body
        let json_body = json!({
            "test": true,
            "message": "Hello from test suite",
            "timestamp": chrono::Utc::now().to_rfc3339()
        });
        let result = test_post_endpoint(&client, &base_url, path, Some(json_body)).await;
        result.print();
        results.push(result);

        // Test with empty body
        let empty_result = test_post_endpoint(&client, &base_url, path, None).await;
        empty_result.print();
        results.push(empty_result);
    }

    // Additional standard tests
    println!("\n  {} Standard POST Tests:\n", "→".blue());

    // Test with various data types
    let complex_body = json!({
        "string": "test",
        "number": 42,
        "float": 3.14159,
        "boolean": true,
        "null": null,
        "array": [1, 2, 3],
        "nested": {
            "key": "value"
        }
    });
    let complex_result = test_post_endpoint(&client, &base_url, "/", Some(complex_body)).await;
    complex_result.print();
    results.push(complex_result);

    // Test with large payload
    let large_body = json!({
        "data": "x".repeat(10000),
        "description": "Large payload test"
    });
    let large_result = test_post_endpoint(&client, &base_url, "/", Some(large_body)).await;
    large_result.print();
    results.push(large_result);

    print_summary(&results);
}

/// Test a single GET endpoint.
async fn test_get_endpoint(client: &Client, base_url: &str, path: &str) -> EndpointTestResult {
    let url = format!("{}{}", base_url, path);
    let timer = Timer::start();

    match client.get(&url).send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            let body = resp.bytes().await.unwrap_or_default();
            let latency = timer.elapsed();

            EndpointTestResult {
                path: path.to_string(),
                method: "GET".to_string(),
                status,
                latency_ms: latency.as_secs_f64() * 1000.0,
                body_size: body.len(),
                success: status < 500,
                error: None,
            }
        }
        Err(e) => EndpointTestResult {
            path: path.to_string(),
            method: "GET".to_string(),
            status: 0,
            latency_ms: timer.elapsed().as_secs_f64() * 1000.0,
            body_size: 0,
            success: false,
            error: Some(e.to_string()),
        },
    }
}

/// Test GET endpoint with query parameters.
async fn test_get_with_query(
    client: &Client,
    base_url: &str,
    path: &str,
    params: &[(&str, &str)],
) -> EndpointTestResult {
    let url = format!("{}{}", base_url, path);
    let timer = Timer::start();

    match client.get(&url).query(params).send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            let body = resp.bytes().await.unwrap_or_default();
            let latency = timer.elapsed();

            let query_str: String = params
                .iter()
                .map(|(k, v)| format!("{}={}", k, v))
                .collect::<Vec<_>>()
                .join("&");

            EndpointTestResult {
                path: format!("{}?{}", path, query_str),
                method: "GET".to_string(),
                status,
                latency_ms: latency.as_secs_f64() * 1000.0,
                body_size: body.len(),
                success: status < 500,
                error: None,
            }
        }
        Err(e) => EndpointTestResult {
            path: path.to_string(),
            method: "GET".to_string(),
            status: 0,
            latency_ms: timer.elapsed().as_secs_f64() * 1000.0,
            body_size: 0,
            success: false,
            error: Some(e.to_string()),
        },
    }
}

/// Test a single POST endpoint.
async fn test_post_endpoint(
    client: &Client,
    base_url: &str,
    path: &str,
    body: Option<Value>,
) -> EndpointTestResult {
    let url = format!("{}{}", base_url, path);
    let timer = Timer::start();

    let request = if let Some(json_body) = body {
        client
            .post(&url)
            .header("Content-Type", "application/json")
            .json(&json_body)
    } else {
        client.post(&url)
    };

    match request.send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            let body = resp.bytes().await.unwrap_or_default();
            let latency = timer.elapsed();

            EndpointTestResult {
                path: path.to_string(),
                method: "POST".to_string(),
                status,
                latency_ms: latency.as_secs_f64() * 1000.0,
                body_size: body.len(),
                success: status < 500,
                error: None,
            }
        }
        Err(e) => EndpointTestResult {
            path: path.to_string(),
            method: "POST".to_string(),
            status: 0,
            latency_ms: timer.elapsed().as_secs_f64() * 1000.0,
            body_size: 0,
            success: false,
            error: Some(e.to_string()),
        },
    }
}

/// Print summary of test results.
fn print_summary(results: &[EndpointTestResult]) {
    let total = results.len();
    let success = results.iter().filter(|r| r.success).count();
    let failed = total - success;

    let total_latency: f64 = results.iter().map(|r| r.latency_ms).sum();
    let avg_latency = if total > 0 {
        total_latency / total as f64
    } else {
        0.0
    };

    println!();
    println!("  {}", "─── Summary ───".white().bold());
    println!(
        "    Total: {} | {} {} | {} {}",
        total.to_string().white(),
        success.to_string().green(),
        "passed".green(),
        failed.to_string().red(),
        "failed".red()
    );
    println!(
        "    Avg Latency: {}",
        format!("{:.2}ms", avg_latency).yellow()
    );
}

/// Test multipart file upload endpoint.
#[allow(dead_code)]
pub async fn test_upload_endpoint(
    client: &Client,
    base_url: &str,
    path: &str,
    files: Vec<(&str, &[u8], &str)>, // (filename, content, content_type)
) -> EndpointTestResult {
    let url = format!("{}{}", base_url, path);
    let timer = Timer::start();

    let mut form = reqwest::multipart::Form::new();

    for (filename, content, content_type) in files {
        let part = reqwest::multipart::Part::bytes(content.to_vec())
            .file_name(filename.to_string())
            .mime_str(content_type)
            .unwrap();
        form = form.part("file", part);
    }

    match client.post(&url).multipart(form).send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            let body = resp.bytes().await.unwrap_or_default();
            let latency = timer.elapsed();

            EndpointTestResult {
                path: path.to_string(),
                method: "POST (multipart)".to_string(),
                status,
                latency_ms: latency.as_secs_f64() * 1000.0,
                body_size: body.len(),
                success: status < 500,
                error: None,
            }
        }
        Err(e) => EndpointTestResult {
            path: path.to_string(),
            method: "POST (multipart)".to_string(),
            status: 0,
            latency_ms: timer.elapsed().as_secs_f64() * 1000.0,
            body_size: 0,
            success: false,
            error: Some(e.to_string()),
        },
    }
}
