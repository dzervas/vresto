[![Go](https://go.dev/images/go-logo-white.svg)](https://go.dev/) 

[Skip to Main Content](https://go.dev/blog/go1.22#main-content)

* [Why Go _arrow_drop_down_](https://go.dev/blog/go1.22) 

  Press Enter to activate/deactivate dropdown

  * [Case Studies](https://go.dev/solutions/case-studies)

    Common problems companies solve with Go
  * [Use Cases](https://go.dev/solutions/use-cases)

    Stories about how and why companies use Go
  * [Security](https://go.dev/security/)

    How Go can help keep you secure by default
* [Learn](https://go.dev/learn/) 

  Press Enter to activate/deactivate dropdown
* [Docs _arrow_drop_down_](https://go.dev/blog/go1.22) 

  Press Enter to activate/deactivate dropdown

  * [Go Spec](https://go.dev/ref/spec)

    The official Go language specification
  * [Go User Manual](https://go.dev/doc)

    A complete introduction to building software with Go
  * [Standard library](https://pkg.go.dev/std)

    Reference documentation for Go's standard library
  * [Release Notes](https://go.dev/doc/devel/release)

    Learn what's new in each Go release
  * [Effective Go](https://go.dev/doc/effective_go)

    Tips for writing clear, performant, and idiomatic Go code
* [Packages](https://pkg.go.dev) 

  Press Enter to activate/deactivate dropdown
* [Community _arrow_drop_down_](https://go.dev/blog/go1.22) 

  Press Enter to activate/deactivate dropdown

  * [Recorded Talks](https://go.dev/talks/)

    Videos from prior events
  * [Meetups _open_in_new_](https://www.meetup.com/pro/go)

    Meet other local Go developers
  * [Conferences _open_in_new_](https://go.dev/wiki/Conferences)

    Learn and network with Go developers from around the world
  * [Go blog](https://go.dev/blog)

    The Go project's official blog.
  * [Go project](https://go.dev/help)

    Get help and stay informed from Go
  * Get connected

    [![](https://go.dev/images/logos/social/google-groups.svg)](https://groups.google.com/g/golang-nuts) [![](https://go.dev/images/logos/social/github.svg)](https://github.com/golang) [![](https://go.dev/images/logos/social/bluesky.svg)](https://bsky.app/profile/golang.org) [![](https://go.dev/images/logos/social/mastodon.svg)](https://hachyderm.io/@golang) [![](https://go.dev/images/logos/social/twitter.svg)](https://twitter.com/golang) [![](https://go.dev/images/logos/social/reddit.svg)](https://www.reddit.com/r/golang/) [![](https://go.dev/images/logos/social/slack.svg)](https://invite.slack.golangbridge.org/) [![](https://go.dev/images/logos/social/stack-overflow.svg)](https://stackoverflow.com/tags/go)

  

[![Go.](https://go.dev/images/go-logo-blue.svg)](https://go.dev/)

* [Why Go _navigate_next_](https://go.dev/blog/go1.22)
* [Learn](https://go.dev/learn/)
* [Docs _navigate_next_](https://go.dev/blog/go1.22)
* [Packages](https://pkg.go.dev)
* [Community _navigate_next_](https://go.dev/blog/go1.22)

  [_navigate_before_Community](https://go.dev/blog/go1.22)

  * [Recorded Talks](https://go.dev/talks/)
  * [Meetups _open_in_new_](https://www.meetup.com/pro/go)
  * [Conferences _open_in_new_](https://go.dev/wiki/Conferences)
  * [Go blog](https://go.dev/blog)
  * [Go project](https://go.dev/help)
  * Get connected

    [![](https://go.dev/images/logos/social/google-groups.svg)](https://groups.google.com/g/golang-nuts) [![](https://go.dev/images/logos/social/github.svg)](https://github.com/golang) [![](https://go.dev/images/logos/social/bluesky.svg)](https://bsky.app/profile/golang.org) [![](https://go.dev/images/logos/social/mastodon.svg)](https://hachyderm.io/@golang) [![](https://go.dev/images/logos/social/twitter.svg)](https://twitter.com/golang) [![](https://go.dev/images/logos/social/reddit.svg)](https://www.reddit.com/r/golang/) [![](https://go.dev/images/logos/social/slack.svg)](https://invite.slack.golangbridge.org/) [![](https://go.dev/images/logos/social/stack-overflow.svg)](https://stackoverflow.com/tags/go)
 

# [The Go Blog](https://go.dev/blog/)

# Go 1.22 is released!

Eli Bendersky, on behalf of the Go team  
 6 February 2024

Today the Go team is thrilled to release Go 1.22, which you can get by visiting the [download page](https://go.dev/dl/).

Go 1.22 comes with several important new features and improvements. Here are some of the notable changes; for the full list, refer to the [release notes](https://go.dev/doc/go1.22).

## Language changes[¶](https://go.dev/blog/go1.22#language-changes)

The long-standing “for” loop gotcha with accidental sharing of loop variables between iterations is now resolved. Starting with Go 1.22, the following code will print “a”, “b”, and “c” in some order:

```go
func main() {
    done := make(chan bool)

    values := []string{"a", "b", "c"}
    for _, v := range values {
        go func() {
            fmt.Println(v)
            done <- true
        }()
    }

    // wait for all goroutines to complete before exiting
    for _ = range values {
        <-done
    }
}
```

For more information about this change and the tooling that helps keep code from breaking accidentally, see the earlier [loop variable blog post](https://go.dev/blog/loopvar-preview).

The second language change is support for ranging over integers:

```go
package main

import "fmt"

func main() {
    for i := range 10 {
        fmt.Println(10 - i)
    }
    fmt.Println("go1.22 has lift-off!")
}
```

The values of `i` in this countdown program go from 0 to 9, inclusive. For more details, please refer to [the spec](https://go.dev/ref/spec#For_range).

## Improved performance[¶](https://go.dev/blog/go1.22#improved-performance)

Memory optimization in the Go runtime improves CPU performance by 1-3%, while also reducing the memory overhead of most Go programs by around 1%.

In Go 1.21, [we shipped](https://go.dev/blog/pgo) profile-guided optimization (PGO) for the Go compiler and this functionality continues to improve. One of the optimizations added in 1.22 is improved devirtualization, allowing static dispatch of more interface method calls. Most programs will see improvements between 2-14% with PGO enabled.

## Standard library additions[¶](https://go.dev/blog/go1.22#standard-library-additions)

* A new [math/rand/v2](https://go.dev/pkg/math/rand/v2) package provides a cleaner, more consistent API and uses higher-quality, faster pseudo-random generation algorithms. See [the proposal](https://go.dev/issue/61716) for additional details.
* The patterns used by [net/http.ServeMux](https://go.dev/pkg/net/http#ServeMux) now accept methods and wildcards.

  For example, the router accepts a pattern like `GET /task/{id}/`, which matches only `GET` requests and captures the value of the `{id}` segment in a map that can be accessed through [Request](https://go.dev/pkg/net/http#Request) values.
* A new `Null[T]` type in [database/sql](https://go.dev/pkg/database/sql) provides a way to scan nullable columns.
* A `Concat` function was added in package [slices](https://go.dev/pkg/slices), to concatenate multiple slices of any type.

---

Thanks to everyone who contributed to this release by writing code and documentation, filing bugs, sharing feedback, and testing the release candidates. Your efforts helped to ensure that Go 1.22 is as stable as possible. As always, if you notice any problems, please [file an issue](https://go.dev/issue/new).

Enjoy Go 1.22!

__Next article:__ [Routing Enhancements for Go 1.22](https://go.dev/blog/routing-enhancements)  
 __Previous article:__ [Share your feedback about developing with Go](https://go.dev/blog/survey2024-h1)  
 __[Blog Index](https://go.dev/blog/all)__

 

[Why Go](https://go.dev/solutions/)   [Use Cases](https://go.dev/solutions/use-cases)   [Case Studies](https://go.dev/solutions/case-studies)

[Get Started](https://go.dev/learn/)   [Playground](https://go.dev/play)   [Tour](https://go.dev/tour/)   [Stack Overflow](https://stackoverflow.com/questions/tagged/go?tab=Newest)   [Help](https://go.dev/help/)

[Packages](https://pkg.go.dev)   [Standard Library](https://go.dev/pkg/)   [About Go Packages](https://pkg.go.dev/about)   [pkg.go.dev API](https://pkg.go.dev/api)

[About](https://go.dev/project)   [Download](https://go.dev/dl/)   [Blog](https://go.dev/blog/)   [Issue Tracker](https://github.com/golang/go/issues)   [Release Notes](https://go.dev/doc/devel/release)   [Brand Guidelines](https://go.dev/brand)   [Code of Conduct](https://go.dev/conduct)

[Connect](https://go.dev/wiki/#the-go-community)   [Bluesky](https://bsky.app/profile/golang.org)   [Mastodon](https://hachyderm.io/@golang)   [Twitter](https://www.twitter.com/golang)   [GitHub](https://github.com/golang)   [Slack](https://invite.slack.golangbridge.org/)   [r/golang](https://reddit.com/r/golang)   [Meetup](https://www.meetup.com/pro/go)   [Golang Weekly](https://golangweekly.com/)

Opens in new window.

![The Go Gopher](https://go.dev/images/gophers/pilot-bust.svg)

* [Copyright](https://go.dev/copyright)
* [Terms of Service](https://go.dev/tos)
* [Privacy Policy](http://www.google.com/intl/en/policies/privacy/)
* [Report an Issue](https://go.dev/s/website-issue)

 [![Google logo](https://go.dev/images/google-white.png)](https://google.com)

go.dev uses cookies from Google to deliver and enhance the quality of its services and to analyze traffic. [Learn more.](https://policies.google.com/technologies/cookies)
