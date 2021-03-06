from flask import jsonify, request, Blueprint

from bmrbapi.utils.configuration import configuration
from bmrbapi.utils.connections import PostgresConnection

# Set up the blueprint
molprobity_endpoints = Blueprint('molprobity', __name__)


@molprobity_endpoints.route('/molprobity/<pdb_id>')
@molprobity_endpoints.route('/molprobity/<pdb_id>/oneline')
def molprobity_oneline(pdb_id):
    """Returns the molprobity data for a PDB ID. """

    return jsonify(get_molprobity_data(pdb_id))


@molprobity_endpoints.route('/molprobity/<pdb_id>/residue')
def molprobity_residue(pdb_id):
    """Returns the molprobity residue data for a PDB ID. """

    return jsonify(get_molprobity_data(pdb_id, residues=request.args.getlist('r')))


def get_molprobity_data(pdb_id, residues=None):
    """ Returns the molprobity data."""

    if residues is None:
        sql = '''SELECT * FROM molprobity.oneline WHERE pdb = lower(%s)'''
        terms = [pdb_id]
    else:
        terms = [pdb_id]
        if not residues:
            sql = '''SELECT * FROM molprobity.residue WHERE pdb = lower(%s);'''
        else:
            sql = '''SELECT * FROM molprobity.residue WHERE pdb = lower(%s) AND ('''
            for item in residues:
                sql += " pdb_residue_no = %s OR "
                terms.append(item)
            sql += " 1=2) ORDER BY model, pdb_residue_no"

    with PostgresConnection() as cur:
        cur.execute(sql, terms)

        res = {"columns": [desc[0] for desc in cur.description], "data": cur.fetchall()}

        if configuration['debug']:
            res['debug'] = cur.query

    return res
