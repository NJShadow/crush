#pragma once
#include <cstdlib>
#include <functional>
#include <memory>
#include <utility>
namespace std {
#ifndef CRUSH_NO_STD_MUTEX_SHIM
struct mutex {
  void lock() {}
  void unlock() {}
  bool try_lock() { return true; }
};
#endif
struct condition_variable {
  void notify_one() {}
  void notify_all() {}
  template <class L, class P> void wait(L&, P pred) { if (!pred()) abort(); }
};
struct thread {
  thread() {}
  template <class F> explicit thread(F&&) { abort(); }   // never reached
  bool joinable() const { return false; }
  void join() {}
  static unsigned hardware_concurrency() { return 1; }
};
template <class R> struct crush_shared_state { R value; bool ready = false; };
template <class R> class future {
 public:
  future() {}
  explicit future(std::shared_ptr<crush_shared_state<R>> s) : s_(std::move(s)) {}
  R get() { if (!s_ || !s_->ready) abort(); return std::move(s_->value); }
  bool valid() const { return (bool)s_; }
 private:
  std::shared_ptr<crush_shared_state<R>> s_;
};
template <class Sig> class packaged_task;
template <class R> class packaged_task<R()> {
 public:
  template <class F> explicit packaged_task(F&& f)
      : f_(std::forward<F>(f)), s_(std::make_shared<crush_shared_state<R>>()) {}
  future<R> get_future() { return future<R>(s_); }
  void operator()() { s_->value = f_(); s_->ready = true; }
 private:
  std::function<R()> f_;
  std::shared_ptr<crush_shared_state<R>> s_;
};
}  // namespace std
