"""Standalone common · IAM / JWT / UserContext stub。

从 monorepo 拆出来的独立版本 · 把原本散落在 a_module / api / c_module 的
公共类型 + JWT 中间件 + UserContext stub 化在一处 · 让 API + worker 能独立跑。

生产前必须接你自己的 IAM 系统(改 ``auth.py`` 内的 stub)。
"""
