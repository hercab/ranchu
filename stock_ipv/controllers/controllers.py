# -*- coding: utf-8 -*-
from odoo import http

# class StockIpv(http.Controller):
#     @http.route('/stock_ipv/stock_ipv/', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/stock_ipv/stock_ipv/objects/', auth='public')
#     def list(self, **kw):
#         return http.request.render('stock_ipv.listing', {
#             'root': '/stock_ipv/stock_ipv',
#             'objects': http.request.env['stock_ipv.stock_ipv'].search([]),
#         })

#     @http.route('/stock_ipv/stock_ipv/objects/<model("stock_ipv.stock_ipv"):obj>/', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('stock_ipv.object', {
#             'object': obj
#         })