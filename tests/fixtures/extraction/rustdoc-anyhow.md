[Docs.rs](https://docs.rs/) 

* [anyhow-1.0.104](https://docs.rs/anyhow/latest/anyhow/ "Flexible concrete Error type built on std::error::Error") 

  * anyhow 1.0.104
  * [Permalink](https://docs.rs/anyhow/1.0.104/anyhow/ "Get a link to this specific version")
  * [Docs.rs crate page](https://docs.rs/crate/anyhow/latest "See anyhow in docs.rs")
  * [MIT](https://spdx.org/licenses/MIT) OR [Apache-2.0](https://spdx.org/licenses/Apache-2.0)
  * 29 September 2026

  * Links
  * [Repository](https://github.com/dtolnay/anyhow)
  * [crates.io](https://crates.io/crates/anyhow "See anyhow in crates.io")
  * [Source](https://docs.rs/crate/anyhow/latest/source/ "Browse source of anyhow-1.0.104")

  * Owners
  * [dtolnay](https://crates.io/users/dtolnay)

  * Dependencies
  * * [futures ^0.3 _dev_](https://docs.rs/futures/^0.3/)
    * [rustversion ^1.0.6 _dev_](https://docs.rs/rustversion/^1.0.6/)
    * [syn ^3 _dev_](https://docs.rs/syn/^3/)
    * [thiserror ^2 _dev_](https://docs.rs/thiserror/^2/)
    * [trybuild ^1.0.108 _dev_](https://docs.rs/trybuild/^1.0.108/)

  * Versions

  * [__100%__ of the crate is documented](https://docs.rs/crate/anyhow/latest)
* [Platform](https://docs.rs/anyhow/latest/anyhow/) 
  * [x86_64-unknown-linux-gnu](https://docs.rs/crate/anyhow/latest/target-redirect/anyhow/)
* [Feature flags](https://docs.rs/crate/anyhow/latest/features "Browse available feature flags of anyhow-1.0.104")

* [docs.rs](https://docs.rs/anyhow/latest/anyhow/)
  * [About docs.rs](https://docs.rs/about)
  * [Badges](https://docs.rs/about/badges)
  * [Builds](https://docs.rs/about/builds)
  * [Metadata](https://docs.rs/about/metadata)
  * [Shorthand URLs](https://docs.rs/about/redirections)
  * [Download](https://docs.rs/about/download)
  * [Rustdoc JSON](https://docs.rs/about/rustdoc-json)
  * [Build queue](https://docs.rs/releases/queue)
  * [Privacy policy](https://foundation.rust-lang.org/policies/privacy-policy/#docs.rs)

* [Rust](https://docs.rs/anyhow/latest/anyhow/)
  * [Rust website](https://www.rust-lang.org/)
  * [The Book](https://doc.rust-lang.org/book/)
  * [Standard Library API Reference](https://doc.rust-lang.org/std/)
  * [Rust by Example](https://doc.rust-lang.org/rust-by-example/)
  * [The Cargo Guide](https://doc.rust-lang.org/cargo/guide/)
  * [Clippy Documentation](https://doc.rust-lang.org/nightly/clippy)

[Skip to main content](https://docs.rs/anyhow/latest/anyhow/#main-content)

[Settings](https://docs.rs/anyhow/latest/settings.html)

[Help](https://docs.rs/anyhow/latest/help.html)

## [Crate anyhow](https://docs.rs/anyhow/latest/anyhow/)

# Crate anyhow Copy item path

[Search](https://docs.rs/anyhow/latest/anyhow/?search=)

[Settings](https://docs.rs/anyhow/latest/settings.html)

[Help](https://docs.rs/anyhow/latest/help.html)

[Source](https://docs.rs/anyhow/latest/src/anyhow/lib.rs.html#1-728)

Expand description

[![github](https://img.shields.io/badge/github-8da0cb?style=for-the-badge&labelColor=555555&logo=github)](https://github.com/dtolnay/anyhow) [![crates-io](https://img.shields.io/badge/crates.io-fc8d62?style=for-the-badge&labelColor=555555&logo=rust)](https://crates.io/crates/anyhow) [![docs-rs](https://img.shields.io/badge/docs.rs-66c2a5?style=for-the-badge&labelColor=555555&logo=docs.rs)](https://docs.rs/anyhow)

  

This library provides [`anyhow::Error`](https://docs.rs/anyhow/latest/anyhow/struct.Error.html "struct anyhow::Error"), a trait object based error type for easy idiomatic error handling in Rust applications.

  

## [§](https://docs.rs/anyhow/latest/anyhow/#details)Details

* Use `Result<T, anyhow::Error>`, or equivalently `anyhow::Result<T>`, as the return type of any fallible function.

  Within the function, use `?` to easily propagate any error that implements the [`std::error::Error`](https://doc.rust-lang.org/core/error/trait.Error.html "trait core::error::Error") trait.

  ```
  use anyhow::Result;

  fn get_cluster_info() -> Result<ClusterMap> {
      let config = std::fs::read_to_string("cluster.json")?;
      let map: ClusterMap = serde_json::from_str(&config)?;
      Ok(map)
  }
  ```
* Attach context to help the person troubleshooting the error understand where things went wrong. A low-level error like “No such file or directory” can be annoying to debug without more context about what higher level step the application was in the middle of.

  ```
  use anyhow::{Context, Result};

  fn main() -> Result<()> {
      ...
      it.detach().context("Failed to detach the important thing")?;

      let content = std::fs::read(path)
          .with_context(|| format!("Failed to read instrs from {}", path))?;
      ...
  }
  ```

  ```console
  Error: Failed to read instrs from ./path/to/instrs.json

  Caused by:
      No such file or directory (os error 2)
  ```
* Downcasting is supported and can be by value, by shared reference, or by mutable reference as needed.

  ```
  // If the error was caused by redaction, then return a
  // tombstone instead of the content.
  match root_cause.downcast_ref::<DataStoreError>() {
      Some(DataStoreError::Censored(_)) => Ok(Poll::Ready(REDACTED_CONTENT)),
      None => Err(error),
  }
  ```
* If using Rust ≥ 1.65, a backtrace is captured and printed with the error if the underlying error type does not already provide its own. In order to see backtraces, they must be enabled through the environment variables described in [`std::backtrace`](https://doc.rust-lang.org/std/backtrace/index.html#environment-variables "mod std::backtrace"):

  * If you want panics and errors to both have backtraces, set `RUST_BACKTRACE=1`;
  * If you want only errors to have backtraces, set `RUST_LIB_BACKTRACE=1`;
  * If you want only panics to have backtraces, set `RUST_BACKTRACE=1` and `RUST_LIB_BACKTRACE=0`.
* Anyhow works with any error type that has an impl of `std::error::Error`, including ones defined in your crate. We do not bundle a `derive(Error)` macro but you can write the impls yourself or use a standalone macro like [thiserror](https://github.com/dtolnay/thiserror).

  ```
  use thiserror::Error;

  #[derive(Error, Debug)]
  pub enum FormatError {
      #[error("Invalid header (expected {expected:?}, got {found:?})")]
      InvalidHeader {
          expected: String,
          found: String,
      },
      #[error("Missing attribute: {0}")]
      MissingAttribute(String),
  }
  ```
* One-off error messages can be constructed using the `anyhow!` macro, which supports string interpolation and produces an `anyhow::Error`.

  ```
  return Err(anyhow!("Missing attribute: {}", missing));
  ```

  A `bail!` macro is provided as a shorthand for the same early return.

  ```
  bail!("Missing attribute: {}", missing);
  ```

  

## [§](https://docs.rs/anyhow/latest/anyhow/#no-std-support)No-std support

In no_std mode, almost all of the same API is available and works the same way. To depend on Anyhow in no_std mode, disable our default enabled “std” feature in Cargo.toml. A global allocator is required.

```toml
[dependencies]
anyhow = { version = "1.0", default-features = false }
```

With versions of Rust older than 1.81, no_std mode may require an additional `.map_err(Error::msg)` when working with a non-Anyhow error type inside a function that returns Anyhow’s error type, as the trait that `?`-based error conversions are defined by is only available in std in those old versions.

## Re-exports[§](https://docs.rs/anyhow/latest/anyhow/#reexports)

`pub use anyhow as format_err;`

## Macros[§](https://docs.rs/anyhow/latest/anyhow/#macros)

[anyhow](https://docs.rs/anyhow/latest/anyhow/macro.anyhow.html "macro anyhow::anyhow")

Construct an ad-hoc error from a string or existing non-`anyhow` error value.

[bail](https://docs.rs/anyhow/latest/anyhow/macro.bail.html "macro anyhow::bail")

Return early with an error.

[ensure](https://docs.rs/anyhow/latest/anyhow/macro.ensure.html "macro anyhow::ensure")

Return early with an error if a condition is not satisfied.

## Structs[§](https://docs.rs/anyhow/latest/anyhow/#structs)

[Chain](https://docs.rs/anyhow/latest/anyhow/struct.Chain.html "struct anyhow::Chain")

Iterator of a chain of source errors.

[Error](https://docs.rs/anyhow/latest/anyhow/struct.Error.html "struct anyhow::Error")

The `Error` type, a wrapper around a dynamic error type.

## Traits[§](https://docs.rs/anyhow/latest/anyhow/#traits)

[Context](https://docs.rs/anyhow/latest/anyhow/trait.Context.html "trait anyhow::Context")

Provides the `context` method for `Result`.

## Functions[§](https://docs.rs/anyhow/latest/anyhow/#functions)

[Ok](https://docs.rs/anyhow/latest/anyhow/fn.Ok.html "fn anyhow::Ok")

Equivalent to `Ok::<_, anyhow::Error>(value)`.

## Type Aliases[§](https://docs.rs/anyhow/latest/anyhow/#types)

[Result](https://docs.rs/anyhow/latest/anyhow/type.Result.html "type anyhow::Result")

`Result<T, Error>`
