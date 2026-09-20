# Decomposition examples

Companion to the "Decomposing a task" section of SKILL.md. The method lives
there; this file shows it applied.

## Shapes tasks come in

| Shape | Ends in | Skeleton |
|---|---|---|
| Retrieve | Reading something | navigate, verify, report the visible content |
| Configure | A changed setting | navigate, select or enter, verify by reading the value back |
| Transact | Something ordered, booked or paid | navigate, select, configure, review, commit boundary |
| Communicate | Something sent | navigate, select the recipient, enter text, commit boundary |

Most requests are one of these. Recognise the shape and the boundary is already
known before the first screen is read.

## A worked decomposition

Request: "find a shop that meets criterion C, add product P with option O to
the cart, and stop before paying."

Backwards from the outcome: the cart showing P with O; before that, P's option
sheet with O chosen; before that, P visible in the shop's menu; before that,
the shop's page; before that, a list of shops matching C; before that, the
app's entry screen.

Decision points: the shop (criterion C - the planner filters, or asks if
several qualify); option O (the planner reads the option labels); the commit
boundary is paying.

```
1. Navigate   goal "open the delivery search"
              end_state "an editable search box is showing"
2. Enter text text_to_type from the user; end_state "the box contains the text"
   Navigate   goal "submit the search"; end_state "a list of shops is showing"
3. Select     jev_observe; keep candidates whose text satisfies C;
              one -> jev_act; several -> ask the user; none -> change route
4. Navigate   goal "open product P"; end_state "P's details or option sheet is showing"
5. Select     jev_observe the option labels; jev_act on O
6. Navigate   goal "add the item to the cart"; end_state "the cart shows P with O"
7. Boundary   stop; report what the cart shows
```

Method here: the backwards walk, the decision points, the boundary, the
end_states written as visible content. Instance: the labels, which appear only
after observation. A task-specific skill records the instance; this section is
the method.

