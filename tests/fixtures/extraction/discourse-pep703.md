[Skip to last reply](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606/131)   [Skip to top](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606/1)

 [Skip to main content](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606#main-container)

[![Discussions on Python.org](https://us1.discourse-cdn.com/flex002/uploads/python1/original/1X/c7591c98caf3b31d4d9c6f322f41ed9d80a50800.png)](https://discuss.python.org/)

    

Please read our [Community Guidelines](https://discuss.python.org/guidelines/) that are tailored to this space in addition to the Python Software Foundation Code of Conduct that we’re bound by.

Reach out to [@moderators](https://discuss.python.org/groups/moderators) with any questions.

   

# [PEP 703: Making the Global Interpreter Lock Optional](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606)

[PEPs](https://discuss.python.org/c/peps/19)

You have selected __0__ posts.

[select all](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606)

    

[cancel selecting](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606)

 

[Jan 2023](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606/1 "Jump to the first post")

1 / 130

Jan 2023

[Oct 2024](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606/131)

## post by ambv on Jan 10, 2023

[![](https://sea2.discourse-cdn.com/flex002/user_avatar/discuss.python.org/ambv/48/25884_2.png)](https://discuss.python.org/u/ambv)

[Łukasz Langa](https://discuss.python.org/u/ambv)     [ambv](https://discuss.python.org/u/ambv)     [CPython core developer](https://discuss.python.org/g/committers)

[Jan 2023](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606 "Post date")

Hi ![:wave:](https://emoji.discourse-cdn.com/apple/wave.png?v=15 ":wave:")

As the sponsor of [@colesbury](https://discuss.python.org/u/colesbury)’s PEP, I’d like to invite you all to discuss the proposal to solve the biggest scalability limitation of CPython by making it possible to build a version of the interpreter which doesn’t use the Global Interpreter Lock.

The document is somewhat of a demanding read as it’s one of the top 10 longest PEPs. That was to be expected though as the problem goes back decades and requires nuance, trade-offs, and careful consideration of alternatives. Thanks in advance for your time reading the PEP and iterating on it with us.

Our hope with this PEP is to get a definitive answer to the GIL question once and for all. We look forward to your feedback!

– Sam Gross and Łukasz Langa

# See the latest version of the PEP

  ![](https://us1.discourse-cdn.com/flex002/uploads/python1/original/2X/8/88d4484552c4beac33d9a0848f523aaa66aad78c.png) [Python Enhancement Proposals (PEPs)](https://peps.python.org/pep-0703/) 

![](https://us1.discourse-cdn.com/flex002/uploads/python1/original/3X/8/3/8354042ac932196b5fde1d1048a76b567decdcdc.png)

### [PEP 703 – Making the Global Interpreter Lock Optional in CPython |...](https://peps.python.org/pep-0703/)

CPython’s global interpreter lock (“GIL”) prevents multiple threads from executing Python code at the same time. The GIL is an obstacle to using multi-core CPUs from Python efficiently. This PEP proposes adding a build configuration (--disable-gil)...

([python.org](http://python.org) uses aggressive front-end caching so you might need to refresh the page your browser to see latest changes)

* [A fast, free threading Python](https://discuss.python.org/t/a-fast-free-threading-python/27903/111)
* [Is Free-Threading Our Only Option?](https://discuss.python.org/t/is-free-threading-our-only-option/91775/100)

[![](https://sea2.discourse-cdn.com/flex002/user_avatar/discuss.python.org/colesbury/48/28031_2.png "Sam Gross") 18](https://discuss.python.org/u/colesbury "colesbury")

[![](https://avatars.discourse-cdn.com/v4/letter/b/e9c0ed/48.png "Barry Scott") 11](https://discuss.python.org/u/barry-scott "barry-scott")

[![](https://sea2.discourse-cdn.com/flex002/user_avatar/discuss.python.org/renejsum/48/2940_2.png "Rene Nejsum") 8](https://discuss.python.org/u/renejsum "renejsum")

[![](https://sea2.discourse-cdn.com/flex002/user_avatar/discuss.python.org/ajoino/48/6907_2.png "Jacob Nilsson") 6](https://discuss.python.org/u/ajoino "ajoino")

[![](https://sea2.discourse-cdn.com/flex002/user_avatar/discuss.python.org/stonebig/48/2796_2.png "Stonebig") 6](https://discuss.python.org/u/stonebig "stonebig")

read   37 min

  

## post by ajoino on Jan 10, 2023

[![](https://sea2.discourse-cdn.com/flex002/user_avatar/discuss.python.org/ajoino/48/6907_2.png)](https://discuss.python.org/u/ajoino)

[Jacob Nilsson](https://discuss.python.org/u/ajoino)     [ajoino](https://discuss.python.org/u/ajoino)

[Jan 2023](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606/2 "Post date")

Exciting stuff!

  

## post by cirospaciari on Jan 10, 2023

[![](https://sea2.discourse-cdn.com/flex002/user_avatar/discuss.python.org/cirospaciari/48/10538_2.png)](https://discuss.python.org/u/cirospaciari)

[Ciro Spaciari](https://discuss.python.org/u/cirospaciari)

[Jan 2023](https://discuss.python.org/t/pep-703-making-the-global-interpreter-lock-optional/22606/3 "Post date")

awesome news!

  

## post by ambv on Jan 10, 2023

  

## post by barry-scott on Jan 10, 2023

  

## post by h-vetinari on Jan 10, 2023

  

## post by barry-scott on Jan 10, 2023

  

## post by jeanas on Jan 10, 2023

  

## post by methane on Jan 11, 2023

  

## post by gregington on Jan 11, 2023

  

## post by colesbury on Jan 11, 2023

  

## post by colesbury on Jan 11, 2023

  

## post by colesbury on Jan 11, 2023

  

## post by petersuter on Jan 11, 2023

  

## post by h-vetinari on Jan 11, 2023

  

## post by encukou on Jan 11, 2023

  

## post by ashbt on Jan 11, 2023

  

## post by ashbt on Jan 11, 2023

  

## post by mattip on Jan 11, 2023

  

## post by ashbt on Jan 11, 2023

 

## Load more posts below
